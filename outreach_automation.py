"""
Automated cold outreach assistant.

What it does:
1. Reads company details from a CSV file.
2. Visits each company's own website to collect public context.
3. Uses an AI email-writing agent to create a formal, easy-to-read email.
4. Sends emails through SMTP, one at a time, with a 10-minute delay by default.

Important:
- Run in dry-run mode first. Only send with --send after reviewing drafts.
- Use this responsibly and follow email marketing laws that apply to you.
- For Gmail SMTP, use an App Password instead of your normal Gmail password.
"""
from __future__ import annotations
from dotenv import load_dotenv
import os

load_dotenv()

datavine_url = os.getenv("DATAVINE_URL")
import argparse
import csv
import json
import re
import smtplib
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_DELAY_SECONDS = 600
DEFAULT_MAX_PAGES_PER_SITE = 4
DEFAULT_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
OUTPUT_DIR = Path("generated_emails")
TRACKING_RECIPIENTS_FILE = Path("tracking_recipients.csv")


@dataclass
class CompanyLead:
    company_name: str
    email: str
    website: str
    main_issue: str
    contact_name: str = ""
    contact_role: str = ""
    send: bool = True


@dataclass
class PageContext:
    url: str
    title: str
    text: str
    html: str = ""


@dataclass
class EmailDraft:
    subject: str
    body: str
    html_body: str = ""


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so the script does not require python-dotenv."""
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def clean_text(value: str, limit: int | None = None) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if limit and len(value) > limit:
        return value[:limit].rsplit(" ", 1)[0] + "..."
    return value


def split_websites(raw: str) -> list[str]:
    if not raw:
        return []
    pieces = re.split(r"[;,]\s*", raw)
    return [normalize_url(piece) for piece in pieces if piece.strip()]


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return raw
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def same_domain(url: str, base_url: str) -> bool:
    return urlparse(url).netloc.lower().removeprefix("www.") == urlparse(base_url).netloc.lower().removeprefix("www.")


def extract_priority_links(html: str, base_url: str, max_links: int) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    priority_words = (
        "about",
        "service",
        "solution",
        "product",
        "pricing",
        "case",
        "customer",
        "contact",
        "team",
        "founder",
        "ceo",
        "leadership",
        "management",
        "executive",
        "director",
    )

    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        text = clean_text(anchor.get_text(" ", strip=True)).lower()
        absolute = urljoin(base_url, href)

        if not absolute.startswith(("http://", "https://")):
            continue
        if not same_domain(absolute, base_url):
            continue
        if any(word in absolute.lower() or word in text for word in priority_words):
            normalized = absolute.split("#", 1)[0].rstrip("/")
            if normalized not in links:
                links.append(normalized)
        if len(links) >= max_links:
            break
    return links


class WebsiteResearchAgent:
    def __init__(self, timeout_seconds: int = 15) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "ColdOutreachResearchBot/1.0 "
                    "(public website context collection; contact sender for removal)"
                )
            }
        )
        self.timeout_seconds = timeout_seconds

    def research(self, websites: Iterable[str], max_pages_per_site: int = DEFAULT_MAX_PAGES_PER_SITE) -> list[PageContext]:
        pages: list[PageContext] = []

        for website in websites:
            if not website:
                continue

            try:
                first_page = self.fetch_page(website)
            except requests.RequestException as exc:
                print(f"[research] Could not fetch {website}: {exc}", file=sys.stderr)
                continue

            if first_page:
                pages.append(first_page)

            links = extract_priority_links(first_page.html, first_page.url, max_pages_per_site - 1)
            for link in links:
                if len([page for page in pages if same_domain(page.url, website)]) >= max_pages_per_site:
                    break
                try:
                    page = self.fetch_page(link)
                except requests.RequestException:
                    continue
                if page and page.url not in {existing.url for existing in pages}:
                    pages.append(page)
                time.sleep(1)

        return pages

    def fetch_page(self, url: str) -> PageContext | None:
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "img", "form"]):
            tag.decompose()

        title = clean_text(soup.title.string if soup.title else "")
        headings = " ".join(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2"])[:10])
        paragraphs = " ".join(node.get_text(" ", strip=True) for node in soup.find_all(["p", "li"])[:80])
        meta_description = ""
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            meta_description = str(meta["content"])

        text = clean_text(" ".join([title, meta_description, headings, paragraphs]), limit=4500)
        return PageContext(url=response.url, title=title, text=text, html=response.text)


class EmailWritingAgent:
    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model

    def create_email(self, lead: CompanyLead, research_pages: list[PageContext]) -> EmailDraft:
        if os.getenv("GROQ_API_KEY"):
            try:
                return self._create_with_groq(lead, research_pages)
            except Exception as exc:
                print(f"[ai] Groq draft failed for {lead.company_name}: {exc}", file=sys.stderr)
                print("[ai] Falling back to a non-AI formal template.", file=sys.stderr)

        return self._fallback_template(lead, research_pages)

    def _create_with_groq(self, lead: CompanyLead, research_pages: list[PageContext]) -> EmailDraft:
        from groq import Groq

        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        sender_name = os.getenv("SENDER_NAME", "Your Name")
        sender_company = os.getenv("SENDER_COMPANY", "Your Company")
        datavine_url = os.getenv("DATAVINE_URL", "https://data-vines.com/")
        datavines_description = os.getenv(
            "DATAVINES_DESCRIPTION",
            (
                "DataVines builds data analytics infrastructure for brands, including data warehousing, "
                "attribution modeling, executive dashboards, predictive analytics, KPI frameworks, "
                "and automated reporting."
            ),
        )

        research_text = "\n\n".join(
            f"Page title: {page.title}\nDetails: {page.text}" for page in research_pages
        )
        research_text = clean_text(research_text, limit=11000)

        instructions = f"""
You are a formal B2B outreach email-writing agent.
Write concise, respectful, easy-to-understand cold outreach for {sender_company}.
Do not invent facts, numbers, clients, partnerships, or results.
Use only the public website context and CSV details provided.
Avoid pressure tactics, spammy wording, fake familiarity, and exaggerated claims.
Make the email feel individually written for this company, not like a reusable template.
Use one specific detail from the company's website when the context supports it.
Do not include the company's website URL or any researched page URL in the email body.
Return only valid JSON with exactly these keys: greeting_name, subject, paragraphs.
The greeting_name value should be an important person from the company website when available, such as the CEO, founder, owner, director, or a relevant leader. Use the provided contact_name if available. If no individual person is clear, use "{lead.company_name} Team".
The subject must sound like a real problem the company may be facing. It must be related to main_issue_from_csv and the website context, but do not copy the CSV issue directly. Use 5 to 9 words, formal and specific. Do not use clickbait, emojis, questions, sales language, or "Re:".
The paragraphs value must be an array of exactly 3 strings.
Do not include "Dear..." inside any paragraph; the program will add the greeting on its own line.
Paragraph 1 must politely explain the specific problem the company appears to have, using a soft and respectful tone.
Paragraph 2 must explain what {sender_company} does and how it can help with that specific problem.
Paragraph 3 must politely ask whether they would be open to a short introductory call or discussion.
Make each paragraph a little fuller than a one-line note, but keep the full email concise and easy to read.
Do not include any generic relevance, opt-out, or "no follow-up" sentence in the body.
Do not write a subject line or signature. The program will add those.
""".strip()

        user_input = {
            "company_name": lead.company_name,
            "contact_name": lead.contact_name,
            "contact_role": lead.contact_role,
            "recipient_email": lead.email,
            "website": lead.website,
            "main_issue_from_csv": lead.main_issue,
            "sender": {
                "name": sender_name,
                "company": sender_company,
                "datavine_url": datavine_url,
                "what_datavines_does": datavines_description,
            },
            "public_website_context": research_text or "No website context could be collected.",
        }

        completion = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(user_input, ensure_ascii=True)},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_completion_tokens=900,
        )

        output_text = completion.choices[0].message.content or ""
        data = parse_json_object(output_text)
        paragraphs = normalize_email_paragraphs(data.get("paragraphs"))
        greeting_name = choose_greeting_name(lead, data.get("greeting_name"))
        subject = clean_problem_subject(data.get("subject"), lead)
        body = compose_email_body(greeting_name, paragraphs, sender_name, sender_company)

        if not subject or not body:
            raise ValueError("AI returned an empty subject or body.")
        return EmailDraft(subject=subject, body=body)

    def _fallback_template(self, lead: CompanyLead, research_pages: list[PageContext]) -> EmailDraft:
        greeting_name = lead.contact_name or f"{lead.company_name} Team"
        website_note = ""
        if research_pages:
            website_note = " After reviewing the information available on your website,"

        sender_name = os.getenv("SENDER_NAME", "Your Name")
        sender_company = os.getenv("SENDER_COMPANY", "Your Company")
        datavine_url = os.getenv("DATAVINE_URL", "https://data-vines.com/")
        datavines_description = os.getenv(
            "DATAVINES_DESCRIPTION",
            (
                "DataVines builds data analytics infrastructure for brands, including data warehousing, "
                "attribution modeling, executive dashboards, predictive analytics, KPI frameworks, "
                "and automated reporting."
            ),
        )
        context_sentence = (
            f"{website_note} it appears that {lead.company_name} may be facing a challenge around {lead.main_issue}."
            if website_note
            else f"It appears that {lead.company_name} may be facing a challenge around {lead.main_issue}."
        )
        paragraphs = [
            f"I hope you are doing well. {context_sentence}",
            (
                f"{datavines_description} For {lead.company_name}, this could help bring scattered information into "
                "a clearer operating view, reduce manual reporting effort, and make decisions easier to act on."
            ),
            (
                "Would you be open to a short introductory call next week to discuss whether this could be useful for your team? "
                "I would be happy to keep the conversation brief, practical, and focused on the priorities that matter most to you."
            ),
        ]
        return EmailDraft(
            subject=fallback_problem_subject(lead),
            body=compose_email_body(greeting_name, paragraphs, sender_name, sender_company),
        )


class SmtpEmailSender:
    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.username = os.getenv("SMTP_USERNAME")
        self.password = os.getenv("SMTP_PASSWORD")
        self.from_email = os.getenv("FROM_EMAIL", self.username or "")
        self.from_name = os.getenv("FROM_NAME", os.getenv("SENDER_NAME", "Your Name"))
        self.reply_to = os.getenv("REPLY_TO_EMAIL", self.from_email)
        self.list_unsubscribe = os.getenv("LIST_UNSUBSCRIBE_EMAIL", "")

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "SMTP_USERNAME": self.username,
                "SMTP_PASSWORD": self.password,
                "FROM_EMAIL": self.from_email,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Missing email configuration: {', '.join(missing)}")

    def send(self, to_email: str, draft: EmailDraft) -> None:
        self.validate()

        message = EmailMessage()
        message["Subject"] = draft.subject
        message["From"] = f"{self.from_name} <{self.from_email}>"
        message["To"] = to_email
        message["Reply-To"] = self.reply_to
        if self.list_unsubscribe:
            message["List-Unsubscribe"] = f"<mailto:{self.list_unsubscribe}>"
        message.set_content(draft.body)
        if draft.html_body:
            message.add_alternative(draft.html_body, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP(self.host, self.port, timeout=30) as server:
            server.starttls(context=context)
            server.login(self.username, self.password)
            server.send_message(message)


def read_leads(csv_path: Path) -> list[CompanyLead]:
    leads: list[CompanyLead] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"company_name", "email", "website", "main_issue"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for row_number, row in enumerate(reader, start=2):
            send_value = (row.get("send") or "yes").strip().lower()
            lead = CompanyLead(
                company_name=clean_text(row.get("company_name", "")),
                email=clean_text(row.get("email", "")),
                website=clean_text(row.get("website", "")),
                main_issue=clean_text(row.get("main_issue", "")),
                contact_name=clean_text(row.get("contact_name", "")),
                contact_role=clean_text(row.get("contact_role", "")),
                send=send_value not in {"no", "false", "0", "skip"},
            )
            if not lead.company_name or not lead.email or not lead.main_issue:
                print(f"[csv] Skipping row {row_number}: company_name, email, and main_issue are required.", file=sys.stderr)
                continue
            leads.append(lead)
    return leads


def parse_json_object(text: str) -> dict:
    """Parse JSON even if a model wraps it in a Markdown code block."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))

    if not isinstance(data, dict):
        raise ValueError("Model did not return a JSON object.")
    return data


def normalize_email_paragraphs(value: object) -> list[str]:
    if isinstance(value, list):
        paragraphs = [clean_email_paragraph(str(item)) for item in value if clean_email_paragraph(str(item))]
    elif isinstance(value, str):
        paragraphs = [clean_email_paragraph(item) for item in re.split(r"\n\s*\n", value) if clean_email_paragraph(item)]
    else:
        raise ValueError("Model did not return a paragraphs list.")

    if len(paragraphs) < 3:
        raise ValueError("Model returned fewer than 3 paragraphs.")
    return paragraphs[:3]


def clean_email_paragraph(value: str) -> str:
    forbidden_sentences = [
        "If this is " + "not " + "relevant, please let me know and I will " + "not " + "follow up.",
    ]
    cleaned = value
    for sentence in forbidden_sentences:
        cleaned = cleaned.replace(sentence, "")
    return clean_text(cleaned)


def choose_greeting_name(lead: CompanyLead, model_value: object = "") -> str:
    if lead.contact_name:
        raw_name = lead.contact_name
    else:
        raw_name = str(model_value or "").strip()

    raw_name = re.sub(r"^dear\s+", "", raw_name, flags=re.IGNORECASE).strip(" ,")
    vague_values = {
        "",
        "team",
        "company team",
        "the team",
        "leadership team",
        "ceo",
        "founder",
        "owner",
        "director",
    }
    if raw_name.lower() in vague_values:
        return f"{lead.company_name} Team"
    return clean_text(raw_name, limit=80)


def compose_email_body(greeting_name: str, paragraphs: list[str], sender_name: str, sender_company: str) -> str:
    signature_role = os.getenv("SIGNATURE_ROLE", "Business Development Representative")
    greeting = f"Dear {greeting_name},"
    body = "\n\n".join(clean_email_paragraph(paragraph) for paragraph in paragraphs[:3])
    return f"{greeting}\n\n{body}\n\nRegards,\n{sender_name}\n{signature_role}\n{sender_company}"


def clean_problem_subject(value: object, lead: CompanyLead) -> str:
    subject = clean_text(str(value or ""), limit=90).strip(" .:-!?\"'")
    subject = re.sub(r"^(subject|re|regarding)\s*:\s*", "", subject, flags=re.IGNORECASE).strip()

    copied_issue = clean_text(lead.main_issue).lower().strip(" .:-!?\"'")
    if not subject or subject.lower() == copied_issue:
        return fallback_problem_subject(lead)

    words = subject.split()
    if len(words) < 4:
        return fallback_problem_subject(lead)
    if len(words) > 10:
        subject = " ".join(words[:10]).strip(" .:-")

    return title_case_subject(subject)


def fallback_problem_subject(lead: CompanyLead) -> str:
    issue = clean_text(lead.main_issue, limit=75).strip(" .:-")
    lower_issue = issue.lower()
    if not issue:
        return "Improving Data Visibility and Reporting"

    if any(word in lower_issue for word in ["manual", "spreadsheet", "report", "reporting"]):
        return "Reducing Manual Reporting and Data Gaps"
    if any(word in lower_issue for word in ["delay", "slow", "response", "turnaround"]):
        return "Reducing Delays in Response Workflows"
    if any(word in lower_issue for word in ["lead", "campaign", "marketing", "attribution"]):
        return "Improving Marketing Data Visibility and Attribution"
    if any(word in lower_issue for word in ["data", "dashboard", "analytics", "kpi", "metrics"]):
        return "Improving Data Visibility for Better Decisions"
    if any(word in lower_issue for word in ["cost", "revenue", "conversion", "growth", "sales"]):
        return "Improving Visibility Into Growth Performance"

    return title_case_subject(f"Improving Visibility Around {issue}")


def title_case_subject(value: str) -> str:
    value = clean_text(value, limit=90).strip(" .:-")
    small_words = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
    words = value.split()
    titled_words = []
    for index, word in enumerate(words):
        lower = word.lower()
        if index > 0 and lower in small_words:
            titled_words.append(lower)
        elif word.isupper():
            titled_words.append(word)
        else:
            titled_words.append(lower[:1].upper() + lower[1:])
    return " ".join(titled_words)


def is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def create_tracking_id() -> str:
    return uuid.uuid4().hex


def attach_html_body(draft: EmailDraft, tracking_base_url: str = "", tracking_id: str = "") -> EmailDraft:
    sender_company = os.getenv("SENDER_COMPANY", "DataVines")
    datavine_url = os.getenv("DATAVINE_URL", "https://data-vines.com/")
    html_parts = [
        "<!doctype html>",
        "<html>",
        "<body style=\"font-family: Arial, sans-serif; font-size: 15px; line-height: 1.55; color: #111111;\">",
    ]

    for block in draft.body.split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        rendered_lines = []
        for line in lines:
            if line == os.getenv("SIGNATURE_ROLE", "Business Development Representative"):
                rendered_lines.append(f"<strong>{escape(line)}</strong>")
            elif line.lower() in {sender_company.lower(), "datavines", "data vines"}:
                safe_url = escape(datavine_url, quote=True)
                rendered_lines.append(f'<a href="{safe_url}">{escape(line)}</a>')
            else:
                rendered_lines.append(escape(line))
        html_parts.append(f"<p>{'<br>'.join(rendered_lines)}</p>")

    if tracking_base_url and tracking_id:
        pixel_url = f"{tracking_base_url.rstrip('/')}/open/{tracking_id}.png"
        html_parts.append(
            f'<img src="{escape(pixel_url, quote=True)}" alt="" width="1" height="1" '
            'style="width:1px;height:1px;opacity:0;border:0;margin:0;padding:0;" />'
        )

    html_parts.extend(["</body>", "</html>"])
    return EmailDraft(subject=draft.subject, body=draft.body, html_body="\n".join(html_parts))


def append_tracking_recipient(path: Path, tracking_id: str, lead: CompanyLead, draft: EmailDraft) -> None:
    path.parent.mkdir(parents=True, exist_ok=True) if path.parent != Path(".") else None
    should_write_header = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["created_at", "tracking_id", "company_name", "email", "subject"],
        )
        if should_write_header:
            writer.writeheader()
        writer.writerow(
            {
                "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "tracking_id": tracking_id,
                "company_name": lead.company_name,
                "email": lead.email,
                "subject": draft.subject,
            }
        )


def save_draft(lead: CompanyLead, draft: EmailDraft, output_dir: Path, tracking_id: str = "") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_company = re.sub(r"[^A-Za-z0-9_.-]+", "_", lead.company_name).strip("_") or "company"
    path = output_dir / f"{timestamp}-{safe_company}.txt"
    tracking_line = f"Tracking-ID: {tracking_id}\n" if tracking_id else ""
    path.write_text(
        f"To: {lead.email}\nSubject: {draft.subject}\n{tracking_line}\n{draft.body}\n",
        encoding="utf-8",
    )
    if draft.html_body:
        path.with_suffix(".html").write_text(draft.html_body, encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    load_dotenv(args.env_file)

    leads = read_leads(Path(args.csv))
    if args.limit:
        leads = leads[: args.limit]

    if not leads:
        print("No leads found.")
        return 0

    research_agent = WebsiteResearchAgent()
    writing_agent = EmailWritingAgent(model=args.model)
    sender = SmtpEmailSender()
    tracking_enabled = args.enable_tracking or is_truthy(os.getenv("ENABLE_OPEN_TRACKING"))
    tracking_base_url = (args.tracking_base_url or os.getenv("TRACKING_BASE_URL", "")).strip().rstrip("/")
    tracking_recipients_path = Path(args.tracking_recipients)

    if tracking_enabled and not tracking_base_url:
        print("[tracking] ENABLE_OPEN_TRACKING is on, but TRACKING_BASE_URL is empty. Emails will be sent without open tracking.")

    sent_count = 0
    for index, lead in enumerate(leads, start=1):
        if not lead.send:
            print(f"[{index}/{len(leads)}] Skipping {lead.company_name} because send=no.")
            continue

        print(f"[{index}/{len(leads)}] Researching {lead.company_name}...")
        websites = split_websites(lead.website)
        research_pages = [] if args.skip_research else research_agent.research(websites, args.max_pages)

        print(f"[{index}/{len(leads)}] Writing email draft for {lead.company_name}...")
        draft = writing_agent.create_email(lead, research_pages)
        tracking_id = create_tracking_id() if tracking_enabled and tracking_base_url else ""
        draft = attach_html_body(draft, tracking_base_url, tracking_id)
        if tracking_id:
            print(f"[tracking] Tracking ID prepared for {lead.company_name}: {tracking_id}")

        draft_path = save_draft(lead, draft, Path(args.output_dir), tracking_id)
        print(f"[draft] Saved: {draft_path}")

        if args.send:
            print(f"[send] Sending to {lead.email}...")
            sender.send(lead.email, draft)
            if tracking_id:
                append_tracking_recipient(tracking_recipients_path, tracking_id, lead, draft)
            sent_count += 1
            print(f"[send] Sent to {lead.email}.")

            if index < len(leads):
                print(f"[rate-limit] Waiting {args.delay_seconds} seconds before the next email.")
                time.sleep(args.delay_seconds)
        else:
            print("[dry-run] Email not sent. Use --send when you are ready.")

    print(f"Done. Sent emails: {sent_count}. Drafts folder: {Path(args.output_dir).resolve()}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research companies and send formal cold outreach emails.")
    parser.add_argument("--csv", default="companies.csv", help="Path to the input CSV file.")
    parser.add_argument("--env-file", default=".env", help="Path to an optional .env file.")
    parser.add_argument("--send", action="store_true", help="Actually send emails. Without this, drafts are generated only.")
    parser.add_argument("--delay-seconds", type=int, default=DEFAULT_DELAY_SECONDS, help="Delay between sent emails.")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES_PER_SITE, help="Maximum website pages to inspect per company.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Groq model to use when GROQ_API_KEY is set.")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N leads.")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Folder for generated draft files.")
    parser.add_argument("--skip-research", action="store_true", help="Skip website research and write from CSV details only.")
    parser.add_argument("--enable-tracking", action="store_true", help="Add an open-tracking pixel when TRACKING_BASE_URL is configured.")
    parser.add_argument("--tracking-base-url", default="", help="Public URL for email_tracker.py, for example https://your-domain.com.")
    parser.add_argument("--tracking-recipients", default=str(TRACKING_RECIPIENTS_FILE), help="CSV file that maps tracking IDs to recipients.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
