# Cold Outreach Automation

This project reads company details from `companies.csv`, checks each company's public website for context, writes a formal 3-paragraph outreach email, and can send the email through SMTP.

The script is intentionally sequential. When `--send` is used, it sends one email, then waits 600 seconds before sending the next one.

Each email uses the CSV `main_issue` plus website research to create a natural problem-focused subject line. The body is personalized from the company's website and follows this structure:

1. The greeting is on its own line, using a CEO, founder, director, or other important person when the company website clearly provides one.
2. The first paragraph politely describes the company's likely problem.
3. The second paragraph explains what DataVines does and how DataVines can help with that problem.
4. The third paragraph politely asks whether they would be open to a short call.

The email then ends with:

```text
Regards,
Your Name
Business Development Representative
DataVines
```

In the HTML email, `Business Development Representative` is bold and `DataVines` links directly to `https://data-vines.com/`.

## CSV format

Use these columns:

```csv
company_name,email,website,main_issue,contact_name,contact_role,send
Example Company,contact@example.com,https://example.com,improving lead response time,Procurement Team,,no
```

Required columns:

- `company_name`
- `email`
- `website`
- `main_issue`

Optional columns:

- `contact_name`
- `contact_role`
- `send`

Set `send` to `no` to skip a row.

## Setup

```bash
python -m pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with your Groq key and SMTP credentials.

The script uses Groq's Python SDK. The default model is:

```env
GROQ_MODEL=openai/gpt-oss-20b
```

For Gmail, use a Gmail App Password. Do not use your normal Gmail password.

## Test without sending

```bash
python outreach_automation.py --csv companies.csv --limit 1
```

This creates a draft in `generated_emails/` and does not send anything.

## Send emails

```bash
python outreach_automation.py --csv companies.csv --send
```

To change the delay:

```bash
python outreach_automation.py --csv companies.csv --send --delay-seconds 600
```

## Open tracking

Open tracking is optional and approximate. It works by adding a tiny image to the HTML version of the email. If the recipient's email client blocks images, or if their provider loads images through a proxy, the result may be missing or imprecise.

Start the tracker locally for testing:

```bash
python email_tracker.py
```

For real tracking, deploy `email_tracker.py` somewhere public and set this in `.env`:

```env
ENABLE_OPEN_TRACKING=yes
TRACKING_BASE_URL=https://your-public-tracker-domain.com
```

When tracking is enabled, the sender writes:

- `tracking_recipients.csv` - maps tracking IDs to companies and emails.
- `tracking_events.csv` - records open events received by the tracker.

Check open status in a browser:

```text
https://your-public-tracker-domain.com/status
```

## Responsible use

Only contact relevant businesses, keep your list clean, honor opt-out requests, and use tracking only where it is lawful for your outreach. Add your business postal address in `.env` where required by local law.
