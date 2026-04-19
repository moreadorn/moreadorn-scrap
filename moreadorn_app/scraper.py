import os
import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, urljoin

# On serverless hosts (Vercel, AWS Lambda, etc.) HOME is read-only, Chrome
# binaries aren't available, and webdriver-manager can't cache drivers.
# Detect it so we can short-circuit Selenium paths before they crash.
IS_SERVERLESS = bool(
    os.environ.get('VERCEL')
    or os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
    or os.environ.get('NETLIFY')
)
# Redirect webdriver-manager's cache to /tmp (the only writable path on Vercel)
# so the import itself doesn't blow up if something touches it indirectly.
os.environ.setdefault('WDM_LOCAL', '1')
os.environ.setdefault('HOME', os.environ.get('HOME') or '/tmp')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Connection': 'keep-alive',
}

EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,7}\b')
PHONE_RE = re.compile(
    r'(?:\+?\d{1,3}[\s\-.]?)?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,6}'
)

JUNK_DOMAINS = {'sentry.io', 'example.com', 'domain.com', 'email.com',
                'yoursite.com', 'company.com', 'wixpress.com'}


def detect_url_type(url):
    u = url.lower()
    if 'google.com/maps' in u or 'maps.google' in u or 'goo.gl/maps' in u:
        return 'google_maps'
    if 'linkedin.com' in u:
        return 'linkedin'
    if 'instagram.com' in u:
        return 'instagram'
    return 'generic'


def scrape_platform(url):
    """Step 1 — scrape the source platform only (no website enrichment)."""
    url_type = detect_url_type(url)
    try:
        if url_type == 'google_maps':
            if IS_SERVERLESS:
                return [{
                    'name': 'Google Maps scraping unavailable on serverless',
                    'phone': '',
                    'website': url,
                    'email': 'Google Maps requires a headless Chrome browser, which is not supported on serverless hosts like Vercel. Run this app on a traditional VM / container to scrape Google Maps.',
                }]
            return scrape_google_maps(url)
        elif url_type == 'linkedin':
            return scrape_linkedin(url)
        elif url_type == 'instagram':
            return scrape_instagram(url)
        else:
            return scrape_generic(url)
    except Exception as e:
        return [{'name': 'Error', 'phone': '', 'website': url, 'email': str(e)}]


def scrape_url(url):
    """Full scrape: platform data + website enrichment (used for direct calls)."""
    results = scrape_platform(url)
    for item in results:
        site = scrape_website_contact(item.get('website', ''))
        item['site_phone'] = site['site_phone']
        item['site_email'] = site['site_email']
    return results


def scrape_website_contact(website_url):
    """Visit a business website and extract all emails and phones from it and its contact page."""
    result = {'site_phone': '', 'site_email': '', 'all_emails': [], 'all_phones': []}
    if not website_url or not website_url.startswith(('http://', 'https://')):
        return result

    skip_domains = ('google.com', 'facebook.com', 'instagram.com',
                    'linkedin.com', 'twitter.com', 'x.com', 'yelp.com')
    if any(d in website_url.lower() for d in skip_domains):
        return result

    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        resp = session.get(website_url, timeout=12, allow_redirects=True)
        soup = BeautifulSoup(resp.text, 'lxml')

        # Find contact/about page links
        contact_urls = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text().lower() + ' ' + href.lower()
            if re.search(r'\bcontact\b|\babout\b|\breach\b|\btouch\b|\bsupport\b', text):
                full = urljoin(website_url, href)
                if full not in contact_urls and website_url.split('/')[2] in full:
                    contact_urls.append(full)

        # Scrape contact pages first, then fallback to main page
        pages_to_scrape = contact_urls[:3] + [website_url]
        seen_emails, seen_phones = set(), set()

        for page_url in pages_to_scrape:
            try:
                if page_url != website_url:
                    pr = session.get(page_url, timeout=10)
                    ps = BeautifulSoup(pr.text, 'lxml')
                    page_text = pr.text
                else:
                    ps = soup
                    page_text = resp.text

                # Emails from mailto: links
                for a in ps.find_all('a', href=re.compile(r'^mailto:', re.I)):
                    e = a['href'].replace('mailto:', '').split('?')[0].strip()
                    if '@' in e and e not in seen_emails:
                        seen_emails.add(e)
                        result['all_emails'].append(e)

                # Emails from text
                for e in _clean_emails(EMAIL_RE.findall(page_text)):
                    if e not in seen_emails:
                        seen_emails.add(e)
                        result['all_emails'].append(e)

                # Phones from tel: links
                for a in ps.find_all('a', href=re.compile(r'^tel:', re.I)):
                    p = a['href'].replace('tel:', '').strip()
                    digits = re.sub(r'\D', '', p)
                    if 7 <= len(digits) <= 15 and p not in seen_phones:
                        seen_phones.add(p)
                        result['all_phones'].append(p)

                # Phones from text
                for p in _clean_phones(PHONE_RE.findall(ps.get_text())):
                    if p not in seen_phones:
                        seen_phones.add(p)
                        result['all_phones'].append(p)

            except Exception:
                continue

        if result['all_emails']:
            result['site_email'] = result['all_emails'][0]
        if result['all_phones']:
            result['site_phone'] = result['all_phones'][0]

    except Exception:
        pass

    return result


def _clean_emails(raw_list):
    result = []
    for e in raw_list:
        domain = e.split('@')[-1].lower()
        if domain in JUNK_DOMAINS:
            continue
        if e.endswith(('.png', '.jpg', '.jpeg', '.svg', '.gif', '.css', '.js', '.webp')):
            continue
        if 'google' in domain or 'facebook' in domain or 'twitter' in domain:
            continue
        result.append(e)
    return result


def _clean_phones(raw_list):
    result = []
    for p in raw_list:
        digits = re.sub(r'\D', '', p)
        if 7 <= len(digits) <= 15:
            result.append(p.strip())
    return result


def _get_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--lang=en-US,en')
    opts.add_argument(
        '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    return driver


def _extract_maps_place(driver):
    from selenium.webdriver.common.by import By

    data = {'name': '', 'phone': '', 'website': '', 'email': ''}
    time.sleep(2.5)

    # Business name
    for sel in ['h1.DUwDvf', 'h1.tAiQdd', 'h1']:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                data['name'] = els[0].text.strip()
                break
        except Exception:
            continue

    # Phone via data-item-id attribute
    try:
        phone_els = driver.find_elements(By.CSS_SELECTOR, '[data-item-id*="phone:tel:"]')
        if phone_els:
            pid = phone_els[0].get_attribute('data-item-id')
            data['phone'] = re.sub(r'^phone:tel:', '', pid)
    except Exception:
        pass

    # Phone fallback: tel: links
    if not data['phone']:
        try:
            tel_links = driver.find_elements(By.CSS_SELECTOR, 'a[href^="tel:"]')
            if tel_links:
                data['phone'] = tel_links[0].get_attribute('href').replace('tel:', '')
        except Exception:
            pass

    # Website
    try:
        ws_els = driver.find_elements(By.CSS_SELECTOR, '[data-item-id="authority"]')
        if ws_els:
            href = ws_els[0].get_attribute('href') or ''
            # Unwrap Google redirect URL
            if 'url?q=' in href or '/url?' in href:
                params = parse_qs(urlparse(href).query)
                data['website'] = params.get('q', params.get('url', [href]))[0]
            else:
                data['website'] = href
    except Exception:
        pass

    # Website fallback
    if not data['website']:
        try:
            ws_links = driver.find_elements(
                By.CSS_SELECTOR,
                'a[aria-label*="website" i], a[aria-label*="Website" i]'
            )
            if ws_links:
                data['website'] = ws_links[0].get_attribute('href') or ''
        except Exception:
            pass

    # Email from page source
    try:
        emails = _clean_emails(EMAIL_RE.findall(driver.page_source))
        if emails:
            data['email'] = emails[0]
    except Exception:
        pass

    return data


def scrape_google_maps(url):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = _get_driver()
    results = []

    try:
        driver.get(url)
        time.sleep(3)

        is_search = any(x in url for x in ['/search/', '/maps/search', 'query='])

        if is_search:
            # Wait for results feed
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'div[role="feed"]')
                    )
                )
            except Exception:
                pass

            # Scroll to load results
            try:
                feed = driver.find_element(By.CSS_SELECTOR, 'div[role="feed"]')
                for _ in range(5):
                    driver.execute_script(
                        'arguments[0].scrollTop += 800', feed
                    )
                    time.sleep(1.5)
            except Exception:
                pass

            # Collect place URLs
            place_urls = []
            try:
                links = driver.find_elements(
                    By.CSS_SELECTOR, 'a[href*="/maps/place/"]'
                )
                for link in links:
                    href = link.get_attribute('href')
                    if href and '/maps/place/' in href and href not in place_urls:
                        place_urls.append(href)
            except Exception:
                pass

            # Visit each place (limit 15)
            for place_url in place_urls[:15]:
                try:
                    driver.get(place_url)
                    d = _extract_maps_place(driver)
                    if d['name']:
                        results.append(d)
                except Exception:
                    continue
        else:
            # Single place page
            d = _extract_maps_place(driver)
            if d['name']:
                results.append(d)

    finally:
        driver.quit()

    if not results:
        results = [{'name': 'No data found', 'phone': '', 'website': url, 'email': ''}]
    return results


def scrape_linkedin(url):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        resp = session.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, 'lxml')

        data = {'name': '', 'phone': '', 'website': url, 'email': ''}

        # Name from h1 or title
        h1 = soup.find('h1')
        title = soup.find('title')
        if h1 and h1.get_text(strip=True):
            data['name'] = h1.get_text(strip=True)
        elif title:
            data['name'] = re.sub(r'\s*[\|\-]\s*LinkedIn.*$', '', title.get_text()).strip()

        # Website from meta
        og_url = soup.find('meta', {'property': 'og:url'})
        if og_url and og_url.get('content'):
            data['website'] = og_url['content']

        # Email and phone from visible text
        emails = _clean_emails(EMAIL_RE.findall(resp.text))
        phones = _clean_phones(PHONE_RE.findall(resp.text))
        if emails:
            data['email'] = emails[0]
        if phones:
            data['phone'] = phones[0]

        return [data]
    except Exception as e:
        return [{'name': 'LinkedIn scrape failed', 'phone': '', 'website': url, 'email': str(e)}]


def scrape_instagram(url):
    try:
        username_match = re.search(r'instagram\.com/([^/?#]+)', url)
        if not username_match:
            return [{'name': 'Invalid Instagram URL', 'phone': '', 'website': url, 'email': ''}]

        username = username_match.group(1).strip('/')
        session = requests.Session()
        session.headers.update({
            **HEADERS,
            'X-IG-App-ID': '936619743392459',
        })

        data = {'name': '', 'phone': '', 'website': '', 'email': ''}

        # Try public API endpoint
        api_url = f'https://www.instagram.com/{username}/?__a=1&__d=dis'
        resp = session.get(api_url, timeout=15)

        if resp.status_code == 200:
            try:
                jd = resp.json()
                user = (
                    jd.get('graphql', {}).get('user', {})
                    or jd.get('data', {}).get('user', {})
                )
                data['name'] = user.get('full_name', '') or username
                data['website'] = user.get('external_url', '')
                data['phone'] = (
                    user.get('business_phone_number', '')
                    or user.get('contact_phone_number', '')
                )
                data['email'] = user.get('business_email', '')

                bio = user.get('biography', '')
                if not data['email']:
                    emails = _clean_emails(EMAIL_RE.findall(bio))
                    if emails:
                        data['email'] = emails[0]
                if not data['phone']:
                    phones = _clean_phones(PHONE_RE.findall(bio))
                    if phones:
                        data['phone'] = phones[0]
            except Exception:
                pass

        # Fallback: HTML scrape
        if not data['name']:
            resp2 = session.get(f'https://www.instagram.com/{username}/', timeout=15)
            soup = BeautifulSoup(resp2.text, 'lxml')

            title_el = soup.find('title')
            if title_el:
                name = re.sub(r'\s*[•@(].*$', '', title_el.get_text()).strip()
                data['name'] = name or username

            desc = soup.find('meta', {'name': 'description'}) or soup.find('meta', {'property': 'og:description'})
            if desc:
                desc_text = desc.get('content', '')
                emails = _clean_emails(EMAIL_RE.findall(desc_text))
                phones = _clean_phones(PHONE_RE.findall(desc_text))
                if emails and not data['email']:
                    data['email'] = emails[0]
                if phones and not data['phone']:
                    data['phone'] = phones[0]

            if not data['website']:
                data['website'] = url

        return [data]
    except Exception as e:
        return [{'name': 'Instagram scrape failed', 'phone': '', 'website': url, 'email': str(e)}]


def scrape_generic(url):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        resp = session.get(url, timeout=15, allow_redirects=True)
        soup = BeautifulSoup(resp.text, 'lxml')

        data = {'name': '', 'phone': '', 'website': url, 'email': ''}

        # Name from JSON-LD structured data
        import json as _json
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                ld = _json.loads(script.string or '{}')
                if isinstance(ld, list):
                    ld = ld[0]
                if ld.get('@type') in ('Organization', 'LocalBusiness', 'Person', 'Corporation'):
                    data['name'] = ld.get('name', '')
                    data['phone'] = ld.get('telephone', '')
                    data['email'] = ld.get('email', '')
                    ws = ld.get('url', '') or ld.get('sameAs', '')
                    if ws:
                        data['website'] = ws if isinstance(ws, str) else ws[0]
                    break
            except Exception:
                continue

        # Name fallback from h1 or title
        if not data['name']:
            h1 = soup.find('h1')
            title = soup.find('title')
            if h1 and h1.get_text(strip=True):
                data['name'] = h1.get_text(strip=True)[:100]
            elif title:
                data['name'] = re.sub(r'\s*[\|\-–]\s*.+$', '', title.get_text()).strip()[:100]

        # tel: links (most reliable phone source)
        if not data['phone']:
            tel_links = soup.find_all('a', href=re.compile(r'^tel:'))
            if tel_links:
                data['phone'] = tel_links[0]['href'].replace('tel:', '').strip()

        # Phone from text
        if not data['phone']:
            phones = _clean_phones(PHONE_RE.findall(soup.get_text()))
            if phones:
                data['phone'] = phones[0]

        # mailto: links
        if not data['email']:
            mailto_links = soup.find_all('a', href=re.compile(r'^mailto:'))
            if mailto_links:
                email = mailto_links[0]['href'].replace('mailto:', '').split('?')[0].strip()
                if '@' in email:
                    data['email'] = email

        # Email from page source
        if not data['email']:
            emails = _clean_emails(EMAIL_RE.findall(resp.text))
            if emails:
                data['email'] = emails[0]

        # Try contact page if still missing info
        if not data['email'] or not data['phone']:
            contact_links = [
                urljoin(url, a['href'])
                for a in soup.find_all('a', href=True)
                if 'contact' in a.get_text().lower() or 'contact' in a['href'].lower()
            ]
            for cp_url in contact_links[:2]:
                try:
                    cp_resp = session.get(cp_url, timeout=10)
                    cp_soup = BeautifulSoup(cp_resp.text, 'lxml')

                    if not data['email']:
                        cp_mailto = cp_soup.find_all('a', href=re.compile(r'^mailto:'))
                        if cp_mailto:
                            e = cp_mailto[0]['href'].replace('mailto:', '').split('?')[0].strip()
                            if '@' in e:
                                data['email'] = e
                        else:
                            cp_emails = _clean_emails(EMAIL_RE.findall(cp_resp.text))
                            if cp_emails:
                                data['email'] = cp_emails[0]

                    if not data['phone']:
                        cp_tel = cp_soup.find_all('a', href=re.compile(r'^tel:'))
                        if cp_tel:
                            data['phone'] = cp_tel[0]['href'].replace('tel:', '').strip()
                        else:
                            cp_phones = _clean_phones(PHONE_RE.findall(cp_soup.get_text()))
                            if cp_phones:
                                data['phone'] = cp_phones[0]
                except Exception:
                    continue

        return [data]
    except Exception as e:
        return [{'name': 'Scrape failed', 'phone': '', 'website': url, 'email': str(e)}]
