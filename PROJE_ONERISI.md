# Project Proposal / Proje Önerisi

**Course:** Web Programming with Python and Git
**Project name:** LexPlain: AI-Powered Contract Simplifier

## Team (4 students)

| # | Name | Student ID | Role |
|---|------|-----------|------|
| 1 | Esra | … | Team lead & Git repo, FastAPI endpoints, page structure and design (HTML/CSS), documentation |
| 2 | Cerenay | … | Text processing, KVKK masking, file reading (PDF/DOCX/photo), frontend logic (JavaScript) |
| 3 | Emir | … | AI integration (Gemini / Claude), analysis pipeline, provider and API tests |
| 4 | Enes | … | Rule engine, legal knowledge base, risk scoring, core tests |

## Problem

People sign rental, subscription and service contracts without understanding them. The language is heavy
("muacceliyet", "müteselsil kefil"), risky clauses are hidden among standard text, and most people do not know
that some clauses are **invalid under Turkish law** (for example, a 4-month deposit or a late-payment penalty in
a residential lease).

## Solution

A web app where the user pastes or uploads a contract (PDF, Word, photo) and selects their role
(tenant/landlord, employee/employer, customer/company, or any other contract with a free-text role). The system:

1. Masks personal data (T.C. ID, IBAN, phone, e-mail) before any processing (KVKK).
2. Splits the contract into clauses.
3. Uses **AI (Google Gemini, optionally Anthropic Claude)** to find clauses against the user, explain them in plain Turkish,
   rate the risk, and suggest a fairer alternative wording.
4. Grounds legal references in a **verified knowledge base** (Turkish Code of Obligations, Consumer
   Protection Law…), so the AI cannot invent law articles. A rule engine also acts as an offline fallback
   and a safety net.
5. Lists missing protections, key dates (with calendar export), questions to ask before signing, and a
   ready-to-send negotiation message, and answers user questions about the contract (chat).

## Why AI is necessary

Contracts are free text with endless phrasings. Keyword rules catch only known patterns. A language model
understands context, rewrites legal language at a high-school reading level, and answers open questions.

## Technology

- **Backend:** Python, FastAPI, Google Gemini API (google-genai), pypdf, python-docx
- **Frontend:** Pure HTML, CSS, JavaScript + Bootstrap 5 (no other library)
- **Version control:** Git / GitHub (feature branches + pull requests)
- **Testing:** pytest (AI calls mocked)
