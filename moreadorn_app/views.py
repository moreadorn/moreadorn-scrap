import csv
import json
import os
import re
import threading
import time
from datetime import timedelta
from urllib.parse import quote

import requests

from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.views.decorators.clickjacking import xframe_options_exempt
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.conf import settings

from .models import (
    AIEmailTemplate,
    CustomEmailTemplate,
    EmailDesign,
    EmailTemplate,
    GeminiAPIKey,
    Lead,
    ScrapeJob,
    WhatsAppConfig,
    WhatsAppTemplate,
)
from .scraper import detect_url_type, scrape_platform, scrape_website_contact


# ── AUTH ──────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect('scrape_leads')
    error = None
    if request.method == 'POST':
        user = authenticate(
            request,
            username=request.POST.get('username', ''),
            password=request.POST.get('password', ''),
        )
        if user:
            login(request, user)
            return redirect(request.GET.get('next', 'scrape_leads'))
        error = 'Invalid username or password.'
    return render(request, 'moreadorn_app/login.html', {'error': error})


def logout_view(request):
    logout(request)
    return redirect('login')


# ── PAGE 1: SCRAPE LEADS ──────────────────────────────

@login_required
def scrape_leads(request):
    all_leads = Lead.objects.all().order_by('-id')[:100]
    return render(request, 'moreadorn_app/scrape_leads.html', {'all_leads': all_leads})


@login_required
def start_scrape(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
        url = body.get('url', '').strip()
    except Exception:
        url = request.POST.get('url', '').strip()
    if not url:
        return JsonResponse({'error': 'URL is required'}, status=400)
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    job = ScrapeJob.objects.create(url=url)
    threading.Thread(target=_run_scrape, args=(str(job.id),), daemon=True).start()
    return JsonResponse({'job_id': str(job.id)})


def _run_scrape(job_id):
    try:
        job = ScrapeJob.objects.get(id=job_id)
        job.status = 'running'
        source_label = {
            'google_maps': 'Google Maps',
            'linkedin': 'LinkedIn',
            'instagram': 'Instagram',
            'generic': 'Website',
        }.get(detect_url_type(job.url), 'Source')
        job.current_step = 2
        job.progress_msg = f'Fetching data from {source_label}...'
        job.save()

        results = scrape_platform(job.url)

        job.current_step = 3
        job.progress_msg = f'Found {len(results)} business(es). Visiting contact pages...'
        job.save(update_fields=['current_step', 'progress_msg'])

        # Build sets of all phones/emails already in DB to detect duplicates
        existing_phones = set()
        existing_emails = set()
        for row in Lead.objects.values('phone', 'site_phone', 'all_phones', 'email', 'site_email', 'all_emails'):
            for p in filter(None, [row['phone'], row['site_phone']]):
                existing_phones.add(p.strip())
            for p in (row['all_phones'] or []):
                if p: existing_phones.add(p.strip())
            for e in filter(None, [row['email'], row['site_email']]):
                existing_emails.add(e.strip().lower())
            for e in (row['all_emails'] or []):
                if e: existing_emails.add(e.strip().lower())

        leads = []
        seen_phones = set()   # dedup within this batch
        seen_emails = set()
        skipped = 0
        skipped_empty = 0

        for item in results:
            site = scrape_website_contact(item.get('website', ''))

            item_phones = set(filter(None, [
                (item.get('phone') or '').strip(),
                (site.get('site_phone') or '').strip(),
                *[(p.strip()) for p in site.get('all_phones', []) if p],
            ]))
            item_emails = set(filter(None, [
                (item.get('email') or '').strip().lower(),
                (site.get('site_email') or '').strip().lower(),
                *[(e.strip().lower()) for e in site.get('all_emails', []) if e],
            ]))

            # Skip leads with no contact info at all (no email, no phone)
            if not item_phones and not item_emails:
                skipped_empty += 1
                continue

            # Skip if any phone or email already exists in DB or current batch
            if (item_phones & existing_phones) or (item_phones & seen_phones):
                skipped += 1
                continue
            if (item_emails & existing_emails) or (item_emails & seen_emails):
                skipped += 1
                continue

            seen_phones.update(item_phones)
            seen_emails.update(item_emails)

            leads.append(Lead(
                job=job,
                name=item.get('name', ''),
                phone=item.get('phone', ''),
                website=item.get('website', ''),
                email=item.get('email', ''),
                site_phone=site.get('site_phone', ''),
                site_email=site.get('site_email', ''),
                all_emails=site.get('all_emails', []),
                all_phones=site.get('all_phones', []),
            ))

        Lead.objects.bulk_create(leads)

        job.status = 'done'
        job.current_step = 4
        bits = []
        if skipped: bits.append(f'{skipped} duplicate(s)')
        if skipped_empty: bits.append(f'{skipped_empty} without contact info')
        skip_msg = f' ({", ".join(bits)} skipped)' if bits else ''
        job.progress_msg = f'Completed — {len(leads)} lead(s) found{skip_msg}.'
        job.completed_at = timezone.now()
        job.save()

    except Exception as exc:
        try:
            job = ScrapeJob.objects.get(id=job_id)
            job.status = 'error'
            job.error_msg = str(exc)
            job.progress_msg = f'Error: {exc}'
            job.save()
        except Exception:
            pass


@login_required
def job_status(request, job_id):
    try:
        job = ScrapeJob.objects.get(id=job_id)
    except ScrapeJob.DoesNotExist:
        return JsonResponse({'error': 'Job not found'}, status=404)

    if job.status == 'running' and job.created_at < timezone.now() - timedelta(minutes=15):
        job.status = 'error'
        job.error_msg = 'Job timed out.'
        job.save()

    data = {
        'status': job.status,
        'current_step': job.current_step,
        'progress_msg': job.progress_msg,
        'error': job.error_msg,
        'results': [],
    }
    if job.status == 'done':
        for lead in job.leads.all():
            data['results'].append({
                'id': lead.id,
                'name': lead.name,
                'phone': lead.phone,
                'website': lead.website,
                'email': lead.email,
                'site_phone': lead.site_phone,
                'site_email': lead.site_email,
                'all_emails': lead.all_emails,
                'all_phones': lead.all_phones,
            })
    return JsonResponse(data)


@login_required
def download_csv(request, job_id):
    """Download CSV — does NOT delete data."""
    try:
        job = ScrapeJob.objects.get(id=job_id)
    except ScrapeJob.DoesNotExist:
        return JsonResponse({'error': 'Job not found'}, status=404)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="leads_{str(job_id)[:8]}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        'Business Name', 'Phone', 'Website', 'Email',
        'Website Phone', 'Website Email', 'All Emails', 'All Phones',
    ])
    for lead in job.leads.all():
        writer.writerow([
            lead.name, lead.phone, lead.website, lead.email,
            lead.site_phone, lead.site_email,
            '|'.join(lead.all_emails or []),
            '|'.join(lead.all_phones or []),
        ])
    return response


# ── PAGE 2: EMAIL TEMPLATE ────────────────────────────

@login_required
def email_content(request):
    if request.method == 'POST':
        form_type = request.POST.get('form_type', 'template')
        if form_type == 'design':
            name = request.POST.get('design_name', '').strip()
            html = request.POST.get('design_html', '').strip()
            if name and html:
                EmailDesign.objects.create(name=name, html=html)
        elif form_type == 'custom':
            subject = request.POST.get('custom_subject', '').strip()
            html = request.POST.get('custom_html', '').strip()
            if subject and html:
                CustomEmailTemplate.objects.create(subject=subject, html=html)
        else:
            subject = request.POST.get('subject', '').strip()
            body = request.POST.get('body', '').strip()
            if subject and body:
                EmailTemplate.objects.create(subject=subject, body=body)
        # POST-Redirect-GET: prevent duplicate creation on browser refresh
        return redirect('email_content')
    templates = EmailTemplate.objects.all()
    designs = EmailDesign.objects.all()
    ai_templates = AIEmailTemplate.objects.all()
    custom_templates = CustomEmailTemplate.objects.all()
    return render(request, 'moreadorn_app/email_content.html', {
        'templates': templates,
        'designs': designs,
        'ai_templates': ai_templates,
        'custom_templates': custom_templates,
    })


@login_required
def delete_email_template(request, pk):
    if request.method == 'POST':
        EmailTemplate.objects.filter(pk=pk).delete()
    return redirect('email_content')


@login_required
def delete_email_design(request, pk):
    if request.method == 'POST':
        EmailDesign.objects.filter(pk=pk).delete()
    return redirect('email_content')


# ── PAGE 3: WHATSAPP TEMPLATE ─────────────────────────

@login_required
def wa_content(request):
    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        if 'file' in request.FILES:
            content = request.FILES['file'].read().decode('utf-8', errors='ignore')
        if content:
            WhatsAppTemplate.objects.create(content=content)
        return redirect('wa_content')
    templates = WhatsAppTemplate.objects.all()
    return render(request, 'moreadorn_app/wa_content.html', {'templates': templates})


@login_required
def delete_wa_template(request, pk):
    if request.method == 'POST':
        WhatsAppTemplate.objects.filter(pk=pk).delete()
    return redirect('wa_content')


# ── PAGE 4: SEND EMAIL ────────────────────────────────

def _search_leads(qs, q):
    """Filter a Lead queryset by name/email/phone/website across all relevant fields."""
    if not q:
        return qs
    return qs.filter(
        Q(name__icontains=q)
        | Q(email__icontains=q)
        | Q(site_email__icontains=q)
        | Q(all_emails__icontains=q)
        | Q(phone__icontains=q)
        | Q(site_phone__icontains=q)
        | Q(all_phones__icontains=q)
        | Q(website__icontains=q)
    )


def _paginate(request, qs, per_page=25):
    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get('page'))
    return page_obj, paginator


@login_required
def send_email_page(request):
    q = (request.GET.get('q') or '').strip()
    leads_qs = _search_leads(Lead.objects.filter(email_sent=False, is_westemail=False), q).order_by('id')
    page_obj, paginator = _paginate(request, leads_qs, per_page=25)
    templates = EmailTemplate.objects.all()
    designs = EmailDesign.objects.all()
    ai_templates = AIEmailTemplate.objects.all()
    custom_templates = CustomEmailTemplate.objects.all()
    return render(request, 'moreadorn_app/send_email.html', {
        'leads': page_obj.object_list,
        'page_obj': page_obj,
        'paginator': paginator,
        'q': q,
        'templates': templates,
        'designs': designs,
        'ai_templates': ai_templates,
        'custom_templates': custom_templates,
    })


def _resolve_template(template_id):
    """Accepts 'ai:N', 'custom:N', 'reg:N', or raw int. Returns (subject, body_html) or (None, None)."""
    if template_id is None or template_id == '':
        tpl = EmailTemplate.objects.order_by('-updated_at').first()
        return (tpl.subject, tpl.body) if tpl else (None, None)
    s = str(template_id)
    if s.startswith('ai:'):
        try:
            t = AIEmailTemplate.objects.get(id=int(s[3:]))
            return (t.title, t.html)
        except AIEmailTemplate.DoesNotExist:
            return (None, None)
    if s.startswith('custom:'):
        try:
            t = CustomEmailTemplate.objects.get(id=int(s[7:]))
            return (t.subject, t.html)
        except CustomEmailTemplate.DoesNotExist:
            return (None, None)
    if s.startswith('reg:'):
        try:
            t = EmailTemplate.objects.get(id=int(s[4:]))
            return (t.subject, t.body)
        except EmailTemplate.DoesNotExist:
            return (None, None)
    try:
        t = EmailTemplate.objects.get(id=int(s))
        return (t.subject, t.body)
    except (EmailTemplate.DoesNotExist, ValueError):
        return (None, None)


def _send_email_logic(lead_ids, template_id=None, design_id=None, name_overrides=None, skip_sent_check=False):
    """Shared logic for sending emails. Returns JsonResponse."""
    subject, body_html = _resolve_template(template_id)
    if not subject:
        return JsonResponse({'error': 'No email template found. Create one first.'}, status=400)

    class _T:
        def __init__(self, s, b):
            self.subject = s
            self.body = b
    template = _T(subject, body_html)

    design = None
    if design_id:
        try:
            design = EmailDesign.objects.get(id=design_id)
        except EmailDesign.DoesNotExist:
            pass

    name_overrides = name_overrides or {}
    qs = Lead.objects.filter(id__in=lead_ids) if skip_sent_check else Lead.objects.filter(id__in=lead_ids, email_sent=False)
    results = []
    for lead in qs:
        if str(lead.id) in name_overrides and name_overrides[str(lead.id)].strip():
            lead.name = name_overrides[str(lead.id)].strip()
            lead.save(update_fields=['name'])
        recipient = lead.best_email
        if not recipient:
            lead.is_westemail = True
            lead.error_mail = 'No email address found'
            lead.save(update_fields=['is_westemail', 'error_mail'])
            results.append({'id': lead.id, 'success': False, 'error': 'No email address'})
            continue
        try:
            if design:
                html_body = design.html.replace('{{content}}', template.body).replace('{{name}}', lead.name or 'there')
            else:
                html_body = _build_email_html(lead.name, template.body)
            msg = EmailMultiAlternatives(
                subject=template.subject,
                body=re.sub(r'<[^>]+>', '', template.body),
                from_email=settings.EMAIL_HOST_USER,
                to=[recipient],
            )
            msg.attach_alternative(html_body, 'text/html')
            msg.send()
            lead.email_sent = True
            lead.email_sent_at = timezone.now()
            lead.is_westemail = False
            lead.error_mail = ''
            lead.save(update_fields=['email_sent', 'email_sent_at', 'is_westemail', 'error_mail'])
            results.append({'id': lead.id, 'success': True, 'to': recipient})
        except Exception as exc:
            err = str(exc)[:500]
            lead.is_westemail = True
            lead.error_mail = err
            lead.save(update_fields=['is_westemail', 'error_mail'])
            results.append({'id': lead.id, 'success': False, 'error': err})
    return JsonResponse({'results': results})


@login_required
def do_send_email(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    return _send_email_logic(
        lead_ids=body.get('lead_ids', []),
        template_id=body.get('template_id'),
        design_id=body.get('design_id'),
    )


def _build_email_html(lead_name, body_content):
    body = (body_content or '').strip()
    if body.lower().startswith(('<!doctype', '<html')):
        # Full HTML email — just substitute {{name}} placeholder
        return body.replace('{{name}}', lead_name or 'there')
    name = lead_name or 'there'
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
body{{margin:0;padding:20px;background:#f0f0f0;font-family:Arial,Helvetica,sans-serif}}
.wrap{{max-width:620px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.12)}}
.hdr{{background:linear-gradient(135deg,#7c3aed,#a855f7);padding:32px 40px}}
.hdr h1{{margin:0;color:#fff;font-size:24px;letter-spacing:-.3px}}
.hdr p{{margin:5px 0 0;color:rgba(255,255,255,.75);font-size:13px}}
.body{{padding:36px 40px;color:#374151;line-height:1.75;font-size:15px}}
.body p{{margin:0 0 16px}}
.footer{{background:#f8f9fa;padding:20px 40px;border-top:1px solid #e9ecef}}
.footer p{{margin:0;font-size:12px;color:#9ca3af;text-align:center;line-height:1.6}}
</style></head>
<body>
<div class="wrap">
  <div class="hdr"><h1>moreAdorn</h1><p>Business Intelligence &amp; Outreach</p></div>
  <div class="body">
    <p>Dear {name},</p>
    {body_content}
  </div>
  <div class="footer">
    <p>&copy; 2025 moreAdorn. All rights reserved.<br>
    To unsubscribe, please reply to this email.</p>
  </div>
</div>
</body></html>"""


# ── PAGE 5: SEND WHATSAPP ─────────────────────────────

@login_required
def send_whatsapp_page(request):
    q = (request.GET.get('q') or '').strip()
    leads_qs = _search_leads(Lead.objects.filter(whatsapp_sent=False, is_westcontact=False), q).order_by('id')
    page_obj, paginator = _paginate(request, leads_qs, per_page=25)
    templates = WhatsAppTemplate.objects.all()
    return render(request, 'moreadorn_app/send_whatsapp.html', {
        'leads': page_obj.object_list,
        'page_obj': page_obj,
        'paginator': paginator,
        'q': q,
        'templates': templates,
    })


def _get_active_whatsapp_config():
    """Return (phone_number_id, access_token) from the currently active WhatsAppConfig row, or (None, None)."""
    cfg = WhatsAppConfig.objects.filter(is_active=True).first()
    if not cfg:
        return None, None
    return cfg.phone_number_id, cfg.access_token


def _wa_send_cloud_api(phone, message):
    """Send a WhatsApp message via Meta Cloud API. Returns (success, error_msg)."""
    phone_id, token = _get_active_whatsapp_config()
    if not token or not phone_id:
        return False, 'WhatsApp API not configured. Add and activate credentials on the WhatsApp API page.'
    url = f'https://graph.facebook.com/v18.0/{phone_id}/messages'
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={
                'messaging_product': 'whatsapp',
                'to': phone,
                'type': 'text',
                'text': {'body': message},
            },
            timeout=20,
        )
        if resp.status_code == 200:
            return True, ''
        try:
            err = resp.json().get('error', {}).get('message') or resp.text[:300]
        except Exception:
            err = resp.text[:300]
        return False, f'{resp.status_code}: {err}'
    except requests.exceptions.RequestException as exc:
        return False, str(exc)[:500]


def _send_whatsapp_logic(lead_ids, template_id=None, name_overrides=None, skip_sent_check=False):
    """Shared logic for AUTO-sending WhatsApp messages via Meta Cloud API."""
    try:
        if template_id:
            template = WhatsAppTemplate.objects.get(id=template_id)
        else:
            template = WhatsAppTemplate.objects.order_by('-updated_at').first()
    except WhatsAppTemplate.DoesNotExist:
        template = None
    if not template:
        return JsonResponse({'error': 'No WhatsApp template found. Create one first.'}, status=400)

    name_overrides = name_overrides or {}
    qs = Lead.objects.filter(id__in=lead_ids) if skip_sent_check else Lead.objects.filter(id__in=lead_ids, whatsapp_sent=False)
    results = []
    for lead in qs:
        if str(lead.id) in name_overrides and name_overrides[str(lead.id)].strip():
            lead.name = name_overrides[str(lead.id)].strip()
            lead.save(update_fields=['name'])
        phone = lead.best_phone
        if not phone:
            # skip — no phone means nothing to send
            lead.is_westcontact = True
            lead.error_contact = 'No phone number found'
            lead.save(update_fields=['is_westcontact', 'error_contact'])
            results.append({'id': lead.id, 'success': False, 'error': 'No phone number (skipped)'})
            continue
        clean = re.sub(r'[^\d+]', '', phone).lstrip('+')
        message = template.content.replace('{{name}}', lead.name or 'there')

        ok, err = _wa_send_cloud_api(clean, message)
        if ok:
            lead.whatsapp_sent = True
            lead.whatsapp_sent_at = timezone.now()
            lead.is_westcontact = False
            lead.error_contact = ''
            lead.save(update_fields=['whatsapp_sent', 'whatsapp_sent_at', 'is_westcontact', 'error_contact', 'name'])
            results.append({'id': lead.id, 'success': True, 'to': clean, 'name': lead.name})
        else:
            lead.is_westcontact = True
            lead.error_contact = err
            lead.save(update_fields=['is_westcontact', 'error_contact', 'name'])
            results.append({'id': lead.id, 'success': False, 'error': err})
    return JsonResponse({'results': results})


@login_required
def do_send_whatsapp(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    return _send_whatsapp_logic(
        lead_ids=body.get('lead_ids', []),
        template_id=body.get('template_id'),
    )


# ── PAGE 6: SENT LEADS / RESEND ───────────────────────

@login_required
def sent_leads(request):
    q = (request.GET.get('q') or '').strip()
    leads_qs = Lead.objects.filter(Q(email_sent=True) | Q(whatsapp_sent=True))
    leads_qs = _search_leads(leads_qs, q).order_by('id')
    page_obj, paginator = _paginate(request, leads_qs, per_page=25)
    email_templates = EmailTemplate.objects.all()
    wa_templates = WhatsAppTemplate.objects.all()
    return render(request, 'moreadorn_app/sent_leads.html', {
        'leads': page_obj.object_list,
        'page_obj': page_obj,
        'paginator': paginator,
        'q': q,
        'email_templates': email_templates,
        'wa_templates': wa_templates,
    })


@login_required
def delete_leads(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    lead_ids = body.get('lead_ids', [])
    deleted, _ = Lead.objects.filter(id__in=lead_ids).delete()
    return JsonResponse({'deleted': deleted})


@login_required
def waste_leads(request):
    """List of leads with failed email and/or contact (is_westemail OR is_westcontact)."""
    q = (request.GET.get('q') or '').strip()
    filt = (request.GET.get('filter') or '').strip()  # '', 'email', 'contact'
    base = Lead.objects.filter(Q(is_westemail=True) | Q(is_westcontact=True))
    if filt == 'email':
        base = Lead.objects.filter(is_westemail=True)
    elif filt == 'contact':
        base = Lead.objects.filter(is_westcontact=True)
    leads_qs = _search_leads(base, q).order_by('-id')
    page_obj, paginator = _paginate(request, leads_qs, per_page=25)
    return render(request, 'moreadorn_app/waste_leads.html', {
        'leads': page_obj.object_list,
        'page_obj': page_obj,
        'paginator': paginator,
        'q': q,
        'filt': filt,
    })


@login_required
def delete_waste_leads(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body or '{}')
    lead_ids = body.get('lead_ids')
    if body.get('all'):
        deleted, _ = Lead.objects.filter(Q(is_westemail=True) | Q(is_westcontact=True)).delete()
    else:
        deleted, _ = Lead.objects.filter(id__in=lead_ids or []).delete()
    return JsonResponse({'deleted': deleted})


@login_required
def delete_all_leads(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    deleted, _ = Lead.objects.all().delete()
    return JsonResponse({'deleted': deleted})


def _create_dummy_lead(name, email, phone):
    """Shared helper used by the public GET dummy-lead endpoints."""
    job, _ = ScrapeJob.objects.get_or_create(
        url='https://dummy.api.local/',
        defaults={
            'status': 'done',
            'current_step': 4,
            'progress_msg': 'Dummy leads created via GET API.',
            'completed_at': timezone.now(),
        },
    )
    lead = Lead.objects.create(
        job=job,
        name=name,
        email=email,
        phone=phone,
        website='https://example.com',
        all_emails=[email],
        all_phones=[phone],
    )
    return JsonResponse({
        'ok': True,
        'message': 'Dummy lead created.',
        'lead': {
            'id': lead.id,
            'name': lead.name,
            'email': lead.email,
            'phone': lead.phone,
            'website': lead.website,
            'created_at': lead.created_at.isoformat() if lead.created_at else None,
        },
    })


def health_check(request):
    """Public health-check endpoint — no login required."""
    return HttpResponse('server is up', content_type='text/plain; charset=utf-8')


def create_dummy_lead(request):
    """Public GET API — no login required. Creates a dummy Lead with
    manavparmar43@gmail.com / 9662771526."""
    return _create_dummy_lead('Dummy Lead (Manav)', 'manavparmar43@gmail.com', '9662771526')


def create_dummy_lead_2(request):
    """Public GET API — no login required. Creates a dummy Lead with
    rhydham.bhalodia122@gmail.com / 7779042233."""
    return _create_dummy_lead('Dummy Lead (Rhydham)', 'rhydham.bhalodia122@gmail.com', '7779042233')


@login_required
def resend_email_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    lead_ids = body.get('lead_ids', [])
    Lead.objects.filter(id__in=lead_ids).update(email_sent=False, email_sent_at=None)
    return _send_email_logic(
        lead_ids=lead_ids,
        template_id=body.get('template_id'),
        design_id=body.get('design_id'),
        name_overrides=body.get('names', {}),
        skip_sent_check=True,
    )


@login_required
def resend_whatsapp_view(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    lead_ids = body.get('lead_ids', [])
    Lead.objects.filter(id__in=lead_ids).update(whatsapp_sent=False, whatsapp_sent_at=None)
    return _send_whatsapp_logic(
        lead_ids=lead_ids,
        template_id=body.get('template_id'),
        name_overrides=body.get('names', {}),
        skip_sent_check=True,
    )


# ── ACCOUNT SETTINGS ─────────────────────────────────

@login_required
def change_password(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    new_pass = body.get('password', '').strip()
    confirm = body.get('confirm', '').strip()
    if not new_pass:
        return JsonResponse({'error': 'Password cannot be empty'})
    if new_pass != confirm:
        return JsonResponse({'error': 'Passwords do not match'})
    request.user.set_password(new_pass)
    request.user.save()
    update_session_auth_hash(request, request.user)
    return JsonResponse({'ok': True})


@login_required
def change_email(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    new_email = body.get('email', '').strip()
    if not new_email:
        return JsonResponse({'error': 'Email cannot be empty'})
    request.user.email = new_email
    request.user.username = new_email
    request.user.save()
    return JsonResponse({'ok': True})


# ── AI EMAIL BUILDER ─────────────────────────────────

def _get_active_api_key():
    active = GeminiAPIKey.objects.filter(is_active=True).first()
    return active.key if active else ''


def _call_gemini(prompt):
    api_key = _get_active_api_key()
    if not api_key:
        raise RuntimeError('No active API key. Please add and activate a Gemini key on the API Keys page.')

    # Try the primary model with retries, then fall back to lighter models if still rate-limited
    models = [settings.GEMINI_MODEL, 'gemini-2.5-flash-lite', 'gemini-1.5-flash']
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 1500},
    }

    last_error = None
    for model in models:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
        for attempt in range(3):
            try:
                response = requests.post(url, json=payload, timeout=30)
                if response.status_code == 429:
                    last_error = 'rate limit (429)'
                    if attempt < 2:
                        time.sleep(3 * (attempt + 1))
                        continue
                    break  # give up on this model, try next one
                response.raise_for_status()
                data = response.json()
                text = data['candidates'][0]['content']['parts'][0]['text'].strip()
                if text.startswith('```'):
                    text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
                    text = re.sub(r'\n?```$', '', text).strip()
                return text
            except requests.exceptions.HTTPError as e:
                last_error = f'HTTP {e.response.status_code}'
                break
            except Exception as e:
                last_error = str(e).split('?key=')[0]
                if attempt < 2:
                    time.sleep(2)
                    continue
                break

    raise RuntimeError(
        f'AI unavailable ({last_error}). Free-tier quota may be exhausted — '
        'wait a minute and try again.'
    )


def _strip_placeholders(text):
    """Remove {{...}} template placeholders from text."""
    return re.sub(r'\{\{[^}]*\}\}', '', text or '').strip()


def _build_rewrite_prompt(template):
    clean_subject = _strip_placeholders(template.subject)
    clean_body = _strip_placeholders(template.body)
    return (
        "You are an expert email copywriter for moreAdorn — a company in the import/export business. "
        "Rewrite the following email content into attractive, personable, professional body paragraphs.\n\n"
        "REQUIREMENTS:\n"
        "- Do NOT include any greeting line (like 'Dear Sir/Mam' or 'Hello') — the greeting is added separately\n"
        "- End with a closing + signature: <p>Warm regards,<br><strong>The moreAdorn Team</strong></p>\n"
        "- Highlight moreAdorn's strength as an import/export business where it naturally fits\n"
        "- Make the message engaging, clear, and concise (around 100-140 words)\n"
        "- Use <p>, <strong>, <br> HTML tags — NO <!DOCTYPE>, <html>, <head>, <body> wrappers\n"
        "- Do NOT use any {{placeholders}} or template variables anywhere\n\n"
        f"Original subject: {clean_subject}\n"
        f"Original body:\n{clean_body}"
    )


def _build_format_prompt(template):
    clean_subject = _strip_placeholders(template.subject)
    clean_body = _strip_placeholders(template.body)
    return (
        "You are an email structuring expert. Take the following email content and reorganize it "
        "into properly-structured professional body paragraphs. "
        "PRESERVE the original wording as much as possible — only fix structure, flow, and spacing.\n\n"
        "STRUCTURE TO PRODUCE:\n"
        "1. Opening paragraph (1-2 sentences introducing context)\n"
        "2. Main body paragraphs (with the original content, cleanly separated)\n"
        "3. Closing paragraph (call-to-action or polite ask)\n"
        "4. Signature: <p>Warm regards,<br><strong>The moreAdorn Team</strong></p>\n\n"
        "REQUIREMENTS:\n"
        "- Do NOT include any greeting line — the greeting is added separately\n"
        "- Include a signature block at the end\n"
        "- Keep original wording, only fix structure and flow\n"
        "- Use <p>, <strong>, <br> HTML tags — NO <!DOCTYPE>, <html>, <head>, <body> wrappers\n"
        "- Do NOT use any {{placeholders}} or template variables\n\n"
        "Context: moreAdorn is in the import/export business.\n\n"
        f"Original subject: {clean_subject}\n"
        f"Original body:\n{clean_body}"
    )


def _run_ai_email(request, prompt_builder):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    template_id = body.get('template_id')
    design_id = body.get('design_id')

    try:
        template = EmailTemplate.objects.get(id=template_id)
    except EmailTemplate.DoesNotExist:
        return JsonResponse({'error': 'Template not found'}, status=404)

    design = None
    if design_id:
        try:
            design = EmailDesign.objects.get(id=design_id)
        except EmailDesign.DoesNotExist:
            pass

    try:
        ai_body = _call_gemini(prompt_builder(template))
    except Exception as exc:
        return JsonResponse({'error': f'AI call failed: {exc}'}, status=500)

    # Scrub any stray {{placeholders}} the AI may have kept in
    ai_body = re.sub(r'\{\{[^}]*\}\}', '', ai_body)
    # Safety net: strip a leading "Dear ..." greeting paragraph if AI included one despite instructions
    ai_body = re.sub(r'^\s*<p>\s*Dear\s+[^<]*?</p>\s*', '', ai_body, count=1, flags=re.IGNORECASE)
    ai_body = re.sub(r'^\s*Dear\s+[^\n<]*?[,.\n]\s*', '', ai_body, count=1, flags=re.IGNORECASE)

    if design:
        # Greeting + signature are inside ai_body; strip any design-level greeting placeholder
        rendered_html = design.html.replace('{{content}}', ai_body).replace('{{name}}', 'Sir/Mam')
    else:
        # Build default wrapper but pass greeting suffix as empty so no duplicate "Dear X,"
        rendered_html = _build_ai_wrapper(ai_body)

    return JsonResponse({
        'preview_html': rendered_html,
        'sendable_html': rendered_html,
        'ai_body': ai_body,
        'subject': template.subject,
    })


def _build_ai_wrapper(ai_body):
    """Wrap AI-generated body in moreAdorn default design. Adds 'Dear Sir/Mam,' greeting."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
body{{margin:0;padding:20px;background:#f0f0f0;font-family:Arial,Helvetica,sans-serif}}
.wrap{{max-width:620px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.12)}}
.hdr{{background:linear-gradient(135deg,#7c3aed,#a855f7);padding:32px 40px}}
.hdr h1{{margin:0;color:#fff;font-size:24px;letter-spacing:-.3px}}
.hdr p{{margin:5px 0 0;color:rgba(255,255,255,.75);font-size:13px}}
.body{{padding:36px 40px;color:#374151;line-height:1.75;font-size:15px}}
.body p{{margin:0 0 16px}}
.footer{{background:#f8f9fa;padding:20px 40px;border-top:1px solid #e9ecef}}
.footer p{{margin:0;font-size:12px;color:#9ca3af;text-align:center;line-height:1.6}}
</style></head>
<body>
<div class="wrap">
  <div class="hdr"><h1>moreAdorn</h1><p>Import &amp; Export Business</p></div>
  <div class="body">
    <p>Dear Sir/Mam,</p>
    {ai_body}
  </div>
  <div class="footer">
    <p>&copy; 2025 moreAdorn. All rights reserved.<br>
    To unsubscribe, please reply to this email.</p>
  </div>
</div>
</body></html>"""


@login_required
def ai_generate_email(request):
    return _run_ai_email(request, _build_rewrite_prompt)


@login_required
def ai_format_email(request):
    return _run_ai_email(request, _build_format_prompt)


@login_required
def ai_save_template(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    body = json.loads(request.body)
    title = body.get('title', '').strip()
    html = body.get('html', '').strip()
    if not title or not html:
        return JsonResponse({'error': 'Title and HTML required'}, status=400)
    t = AIEmailTemplate.objects.create(title=title, html=html)
    return JsonResponse({'ok': True, 'id': t.id, 'subject': t.title})


@login_required
def delete_ai_template(request, pk):
    if request.method == 'POST':
        AIEmailTemplate.objects.filter(pk=pk).delete()
    return redirect('email_content')


@xframe_options_exempt
@login_required
def preview_ai_template(request, pk):
    try:
        tpl = AIEmailTemplate.objects.get(pk=pk)
    except AIEmailTemplate.DoesNotExist:
        return HttpResponse('<p style="padding:20px">Not found</p>', status=404)
    return HttpResponse(tpl.html, content_type='text/html; charset=utf-8')


@login_required
def delete_custom_template(request, pk):
    if request.method == 'POST':
        CustomEmailTemplate.objects.filter(pk=pk).delete()
    return redirect('email_content')


@xframe_options_exempt
@login_required
def preview_custom_template(request, pk):
    try:
        tpl = CustomEmailTemplate.objects.get(pk=pk)
    except CustomEmailTemplate.DoesNotExist:
        return HttpResponse('<p style="padding:20px">Not found</p>', status=404)
    html = tpl.html.replace('{{name}}', '{Business Name}')
    # If the uploaded HTML isn't a full document, render inside a minimal wrapper so it previews cleanly.
    if not html.strip().lower().startswith(('<!doctype', '<html')):
        html = _minimal_preview(html)
    return HttpResponse(html, content_type='text/html; charset=utf-8')


# ── API KEY MANAGEMENT ────────────────────────────────

@login_required
def api_keys_page(request):
    keys = GeminiAPIKey.objects.all()
    return render(request, 'moreadorn_app/api_keys.html', {'keys': keys})


@login_required
def add_api_key(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
    except Exception:
        body = {}
    key = (body.get('key') or request.POST.get('key') or '').strip()
    label = (body.get('label') or request.POST.get('label') or '').strip()

    if not key:
        return JsonResponse({'error': 'API key is required'}, status=400)

    # Gemini API keys from Google AI Studio always start with "AIza"
    if not key.startswith('AIza') or len(key) < 30:
        return JsonResponse({
            'error': 'Only Gemini API keys are accepted. Gemini keys start with "AIza..." and are issued by Google AI Studio.'
        }, status=400)

    # Validate by pinging the Gemini endpoint with this key
    try:
        test_url = f'https://generativelanguage.googleapis.com/v1beta/models?key={key}'
        r = requests.get(test_url, timeout=10)
        if r.status_code == 401 or r.status_code == 403:
            return JsonResponse({
                'error': 'Only Gemini API keys are accepted. This key was rejected by Google AI Studio.'
            }, status=400)
        if r.status_code == 400:
            return JsonResponse({
                'error': 'Invalid API key format. Only Gemini keys are accepted.'
            }, status=400)
        # 200 or 429 (rate limited but valid) → accept
    except requests.exceptions.RequestException:
        # Network error — still allow, format check already passed
        pass

    if GeminiAPIKey.objects.filter(key=key).exists():
        return JsonResponse({'error': 'This API key is already saved.'}, status=400)

    # If no key is active yet, make this one active
    make_active = not GeminiAPIKey.objects.filter(is_active=True).exists()
    GeminiAPIKey.objects.create(key=key, label=label, is_active=make_active)
    return JsonResponse({'ok': True, 'active': make_active})


@login_required
def activate_api_key(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    GeminiAPIKey.objects.update(is_active=False)
    updated = GeminiAPIKey.objects.filter(pk=pk).update(is_active=True)
    if not updated:
        return JsonResponse({'error': 'Key not found'}, status=404)
    return JsonResponse({'ok': True, 'active': True})


@login_required
def deactivate_api_key(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    updated = GeminiAPIKey.objects.filter(pk=pk).update(is_active=False)
    if not updated:
        return JsonResponse({'error': 'Key not found'}, status=404)
    return JsonResponse({'ok': True, 'active': False})


@login_required
def delete_api_key(request, pk):
    if request.method == 'POST':
        GeminiAPIKey.objects.filter(pk=pk).delete()
    return redirect('api_keys')


# ── WHATSAPP CONFIG (Meta Cloud API credentials) ────

@login_required
def whatsapp_config_page(request):
    configs = WhatsAppConfig.objects.all()
    return render(request, 'moreadorn_app/whatsapp_config.html', {'configs': configs})


@login_required
def add_whatsapp_config(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
    except Exception:
        body = {}
    phone_id = (body.get('phone_number_id') or '').strip()
    token = (body.get('access_token') or '').strip()
    label = (body.get('label') or '').strip()

    if not phone_id or not token:
        return JsonResponse({'error': 'Both Phone Number ID and Access Token are required.'}, status=400)
    if not phone_id.isdigit():
        return JsonResponse({'error': 'Phone Number ID must be numeric (the numeric ID from Meta, not the phone number itself).'}, status=400)
    if len(token) < 20:
        return JsonResponse({'error': 'Access Token looks invalid — tokens are typically 100+ characters.'}, status=400)

    # Validate with Meta: fetch phone-number metadata
    try:
        test_url = f'https://graph.facebook.com/v18.0/{phone_id}?fields=display_phone_number&access_token={token}'
        r = requests.get(test_url, timeout=10)
        if r.status_code in (401, 403):
            return JsonResponse({'error': 'Meta rejected these credentials. Double-check the token and phone number ID.'}, status=400)
        if r.status_code == 400:
            try:
                err = r.json().get('error', {}).get('message') or r.text[:200]
            except Exception:
                err = r.text[:200]
            return JsonResponse({'error': f'Meta validation failed: {err}'}, status=400)
    except requests.exceptions.RequestException:
        # Network error — allow save, user can test by sending
        pass

    if WhatsAppConfig.objects.filter(phone_number_id=phone_id, access_token=token).exists():
        return JsonResponse({'error': 'This configuration is already saved.'}, status=400)

    make_active = not WhatsAppConfig.objects.filter(is_active=True).exists()
    WhatsAppConfig.objects.create(
        phone_number_id=phone_id,
        access_token=token,
        label=label,
        is_active=make_active,
    )
    return JsonResponse({'ok': True, 'active': make_active})


@login_required
def activate_whatsapp_config(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    WhatsAppConfig.objects.update(is_active=False)
    updated = WhatsAppConfig.objects.filter(pk=pk).update(is_active=True)
    if not updated:
        return JsonResponse({'error': 'Config not found'}, status=404)
    return JsonResponse({'ok': True, 'active': True})


@login_required
def deactivate_whatsapp_config(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    updated = WhatsAppConfig.objects.filter(pk=pk).update(is_active=False)
    if not updated:
        return JsonResponse({'error': 'Config not found'}, status=404)
    return JsonResponse({'ok': True, 'active': False})


@login_required
def delete_whatsapp_config(request, pk):
    if request.method == 'POST':
        WhatsAppConfig.objects.filter(pk=pk).delete()
    return redirect('whatsapp_config')


# ── EMAIL TEMPLATE PREVIEW ────────────────────────────

def _minimal_preview(body_html):
    """Render a template body inside a neutral minimal wrapper — no moreAdorn header/footer branding.
    If the body is already a full HTML document (AI-crafted template), return it as-is."""
    body = (body_html or '').strip()
    if body.lower().startswith(('<!doctype', '<html')):
        return body.replace('{{name}}', '{Business Name}')
    body = body.replace('{{name}}', '{Business Name}')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
body{{margin:0;padding:30px 20px;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#1f2937;line-height:1.75;font-size:15px}}
.content{{max-width:640px;margin:0 auto;background:#fff;border-radius:12px;padding:32px 36px;box-shadow:0 2px 14px rgba(0,0,0,0.06)}}
.content p{{margin:0 0 16px}}
.content a{{color:#7c3aed}}
</style>
</head>
<body>
<div class="content">
{body}
</div>
</body>
</html>"""


@xframe_options_exempt
@login_required
def preview_email_template(request, pk):
    # Accept either a regular EmailTemplate PK or a composite like ai:N / reg:N
    subject, body_html = _resolve_template(pk)
    if not subject:
        return HttpResponse(
            '<p style="font-family:sans-serif;padding:20px;color:#666">Template not found.</p>',
            status=404,
        )
    design_id = request.GET.get('design_id')
    if design_id:
        try:
            design = EmailDesign.objects.get(pk=design_id)
            html = design.html.replace('{{content}}', body_html).replace('{{name}}', '{Business Name}')
        except EmailDesign.DoesNotExist:
            html = _minimal_preview(body_html)
    else:
        html = _minimal_preview(body_html)
    return HttpResponse(html, content_type='text/html; charset=utf-8')


@xframe_options_exempt
@login_required
def preview_email_design(request, pk):
    try:
        design = EmailDesign.objects.get(pk=pk)
    except EmailDesign.DoesNotExist:
        return HttpResponse(
            '<p style="font-family:sans-serif;padding:20px;color:#666">Design not found.</p>',
            status=404,
        )
    sample_body = '<p style="font-family:Arial,sans-serif;font-size:15px;color:#374151;line-height:1.7">This is a <strong>sample email body</strong>. Your actual content will appear here when you select a template on the Send Email page.</p><p style="font-family:Arial,sans-serif;font-size:15px;color:#374151;line-height:1.7;margin-top:12px">Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.</p>'
    html = design.html.replace('{{content}}', sample_body).replace('{{name}}', '{Business Name}')
    return HttpResponse(html, content_type='text/html; charset=utf-8')
