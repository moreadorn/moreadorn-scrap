import uuid
from django.db import models


class ScrapeJob(models.Model):
    STATUS = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('error', 'Error'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    url =models.TextField()
    status = models.TextField(choices=STATUS, default='pending')
    current_step = models.IntegerField(default=1)
    progress_msg = models.TextField( blank=True, default='Initializing...')
    error_msg = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class Lead(models.Model):
    job = models.ForeignKey(ScrapeJob, on_delete=models.CASCADE, related_name='leads')
    name = models.TextField()
    phone = models.TextField()
    website = models.TextField()
    email = models.TextField()
    site_phone = models.TextField()
    site_email = models.TextField()
    all_emails = models.JSONField(default=list, blank=True)
    all_phones = models.JSONField(default=list, blank=True)
    email_sent = models.BooleanField(default=False)
    whatsapp_sent = models.BooleanField(default=False)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    whatsapp_sent_at = models.DateTimeField(null=True, blank=True)
    is_westemail = models.BooleanField(default=False)
    is_westcontact = models.BooleanField(default=False)
    error_mail = models.TextField(blank=True, default='')
    error_contact = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['id']

    @property
    def best_email(self):
        if self.all_emails:
            return self.all_emails[0]
        return self.site_email or self.email

    @property
    def best_phone(self):
        if self.all_phones:
            return self.all_phones[0]
        return self.site_phone or self.phone

    @property
    def display_emails(self):
        seen = []
        for e in ([self.email, self.site_email] + list(self.all_emails or [])):
            if e and e not in seen:
                seen.append(e)
        return seen

    @property
    def display_phones(self):
        seen = []
        for p in ([self.phone, self.site_phone] + list(self.all_phones or [])):
            if p and p not in seen:
                seen.append(p)
        return seen


class EmailTemplate(models.Model):
    subject = models.TextField()
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']


class AIEmailTemplate(models.Model):
    """AI-generated email templates — kept separate from manually authored EmailTemplate."""
    title = models.TextField()
    html = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']


class CustomEmailTemplate(models.Model):
    """User-uploaded full templates — subject + ready-to-send HTML (design + content merged).
    No AI formatting step. Selectable on the Send Email page alongside AI templates."""
    subject = models.TextField()
    html = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']


class GeminiAPIKey(models.Model):
    """User-provided Gemini API keys. Only one can be active at a time."""
    key = models.TextField()
    label = models.TextField()
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_active', '-created_at']

    @property
    def masked(self):
        if len(self.key) <= 10:
            return '*' * len(self.key)
        return self.key[:6] + '...' + self.key[-4:]


class WhatsAppConfig(models.Model):
    """User-provided Meta WhatsApp Cloud API credentials. Only one can be active."""
    phone_number_id = models.TextField()
    access_token = models.TextField()
    label = models.TextField()
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_active', '-created_at']

    @property
    def masked_token(self):
        t = self.access_token
        if len(t) <= 10:
            return '*' * len(t)
        return t[:6] + '...' + t[-4:]


class EmailDesign(models.Model):
    name = models.TextField()
    html = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']


class WhatsAppTemplate(models.Model):
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
