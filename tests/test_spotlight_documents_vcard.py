"""Listing Spotlight 10/24: documents are emailed (never downloaded on the spot) and Save contact
carries the agent's photo."""
from types import SimpleNamespace

from app.services import personal_email_service, vcard_service
from app.services.property_website_service import published_documents


def test_documents_email_lists_every_document_link():
    v = {"first_name": "Gina", "property_address": "26 Old Gulph Rd", "site_url": "https://26-old-gulph-rd.openhousepal.com",
         "documents": [{"label": "Seller's disclosure", "url": "https://cdn.example.com/a.pdf"}, {"label": "Brochure", "url": "https://cdn.example.com/b.pdf"}],
         "agent_name": "Quinn Reviewer", "agent_phone": "(610) 555-0134"}
    assert "spotlight_documents" in personal_email_service.PERSONAL_TEMPLATES
    subject, html, text = personal_email_service.render("spotlight_documents", "The documents for 26 Old Gulph Rd", v, link="")
    assert subject == "The documents for 26 Old Gulph Rd"
    assert "Here are the documents for 26 Old Gulph Rd" in text
    for url in ("https://cdn.example.com/a.pdf", "https://cdn.example.com/b.pdf", "https://26-old-gulph-rd.openhousepal.com"):
        assert url in text and f'href="{url}"' in html
    assert "(610) 555-0134" in text


def test_documents_email_single_document_wording():
    v = {"first_name": "Gina", "property_address": "26 Old Gulph Rd", "documents": [{"label": "Brochure", "url": "https://x.example.com/b.pdf"}]}
    _, _, text = personal_email_service.render("spotlight_documents", "s", v, link="")
    assert "Here is the Brochure for 26 Old Gulph Rd you asked for:" in text


def test_only_listed_documents_count():
    site = SimpleNamespace(published_snapshot={"documents": [
        {"label": "Brochure", "url": "https://x.example.com/b.pdf"},
        {"label": "", "display_name": "Floor plans", "url": "https://x.example.com/f.pdf"},
        {"label": "Bad", "url": "javascript:alert(1)"},
        "nonsense",
    ]})
    docs = published_documents(site)
    assert [d["label"] for d in docs] == ["Brochure", "Floor plans"]


def test_spotlight_vcard_has_photo_and_text_number():
    agent = {"name": "Quinn Reviewer", "title": "Realtor", "brokerage": "Main Line Test Realty", "phone": "(610) 555-0134",
             "text_phone": "(610) 555-0999", "email": "qa@example.com", "hub_url": "https://openhousepal.com/quinn"}
    text = vcard_service.build_agent_vcard_text(agent, photo_base64="A" * 300, note="Listing agent for 1 Main St")
    lines = text.split("\r\n")
    assert lines[0] == "BEGIN:VCARD" and "END:VCARD" in lines
    assert "N:Reviewer;Quinn;;;" in lines and "FN:Quinn Reviewer" in lines
    assert "TEL;TYPE=CELL:(610) 555-0134" in lines and "item9.X-ABLabel:Text" in lines
    assert any(l.startswith("PHOTO;ENCODING=b;TYPE=JPEG:") for l in lines)
    assert all(len(l.encode()) <= 75 for l in lines)  # folded


def test_spotlight_vcard_without_photo_or_second_number():
    text = vcard_service.build_agent_vcard_text({"name": "Cher", "phone": "6105550134", "text_phone": "610-555-0134"})
    assert "PHOTO" not in text and "item9" not in text and "N:;Cher;;;" in text
