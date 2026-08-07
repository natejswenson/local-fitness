"""Tests for SMTP delivery of the evening brief.

The load-bearing test in this file is
``test_every_cid_reference_resolves_to_an_attached_image``. Everything else
here is ordinary config handling; that one guards the invariant the whole
image path rests on, and it is the invariant that broke silently on every
other route we considered — a message whose HTML references ``cid:chart0``
with nothing attached under that name renders as a broken-image icon in the
inbox while every unit test and browser preview still looks correct.

The socket itself is not tested: ``send``'s body is ``smtplib`` glue, and a
test around it would only assert that a mock replays its own canned value.
Which transport gets chosen for a given port IS tested, because that is a real
branch with a security consequence.
"""
from __future__ import annotations

import email
import re
from email import policy

import pytest

from local_fitness.agent import mailer
from local_fitness.agent.email_render import chart_cid

CFG = mailer.MailConfig(
    host="smtp.example.com", port=465, user="me@example.com",
    password="secret", to=("me@example.com",), from_addr="me@example.com",
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"


def parse(msg) -> email.message.EmailMessage:
    return email.message_from_bytes(msg.as_bytes(), policy=policy.default)


def build(charts: dict[str, bytes], html: str = "<p>hi</p>", cfg=CFG):
    return mailer.build_message("Evening Brief · 2026-08-07", html, "hi", charts, cfg)


# --- MIME structure --------------------------------------------------------

def test_structure_is_alternative_wrapping_related():
    # cid: resolves only when the images are *related* to the HTML part
    # specifically. Attached as siblings of the whole message they show up as
    # loose downloads instead of inline figures.
    parsed = parse(build({"0": PNG}))
    assert parsed.get_content_type() == "multipart/alternative"
    kinds = [p.get_content_type() for p in parsed.get_payload()]
    assert kinds == ["text/plain", "multipart/related"]
    related = parsed.get_payload()[1].get_payload()
    assert [p.get_content_type() for p in related] == ["text/html", "image/png"]


def test_every_cid_reference_resolves_to_an_attached_image():
    html = "".join(f'<img src="cid:{chart_cid(i)}">' for i in range(3))
    parsed = parse(build({str(i): PNG for i in range(3)}, html=html))

    body = parsed.get_body(preferencelist=("html",)).get_content()
    refs = set(re.findall(r'src="cid:([^"]+)"', body))
    cids = {p["Content-ID"].strip("<>") for p in parsed.walk() if p.get("Content-ID")}

    assert refs == cids == {"chart0", "chart1", "chart2"}


def test_content_id_header_is_bracketed_but_the_url_is_not():
    # RFC 2392: the header carries angle brackets, the `cid:` URL omits them.
    # Getting this backwards breaks images in some clients and not others.
    parsed = parse(build({"0": PNG}))
    img = next(p for p in parsed.walk() if p.get_content_type() == "image/png")
    assert img["Content-ID"] == f"<{chart_cid(0)}>"


def test_attachment_bytes_survive_the_round_trip():
    # Full fidelity is the entire reason this path exists rather than the
    # connector one — a re-encoded or truncated PNG is the failure it avoids.
    parsed = parse(build({"0": PNG}))
    img = next(p for p in parsed.walk() if p.get_content_type() == "image/png")
    assert img.get_payload(decode=True) == PNG


def test_charts_are_ordered_numerically_not_lexicographically():
    # str keys: a plain sort puts "10" between "1" and "2", so chart 10 would
    # attach in the wrong position relative to its takeaway.
    parsed = parse(build({str(i): PNG for i in range(11)}))
    order = [p["Content-ID"].strip("<>") for p in parsed.walk() if p.get("Content-ID")]
    assert order == [f"chart{i}" for i in range(11)]


def test_a_brief_with_no_charts_still_builds_a_valid_message():
    parsed = parse(build({}))
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == "hi"
    assert parsed.get_body(preferencelist=("html",)) is not None
    assert not [p for p in parsed.walk() if p.get_content_type() == "image/png"]


def test_headers_carry_subject_sender_and_recipients():
    cfg = mailer.MailConfig(**{**CFG.__dict__, "to": ("a@x.com", "b@x.com")})
    parsed = parse(build({}, cfg=cfg))
    assert parsed["Subject"] == "Evening Brief · 2026-08-07"
    assert parsed["From"] == "me@example.com"
    assert parsed["To"] == "a@x.com, b@x.com"
    assert parsed["Date"] and parsed["Message-ID"]


def test_both_alternatives_are_present_and_distinct():
    parsed = parse(build({}, html="<p>rich</p>"))
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == "hi"
    assert "rich" in parsed.get_body(preferencelist=("html",)).get_content()


# --- config ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LOCAL_FITNESS_SMTP_HOST", "LOCAL_FITNESS_SMTP_PORT",
              "LOCAL_FITNESS_SMTP_USER", "LOCAL_FITNESS_SMTP_PASSWORD",
              "LOCAL_FITNESS_BRIEF_EMAIL_TO", "LOCAL_FITNESS_BRIEF_EMAIL_FROM"):
        monkeypatch.delenv(k, raising=False)


def _configured(monkeypatch, **over):
    env = {"LOCAL_FITNESS_SMTP_USER": "me@gmail.com",
           "LOCAL_FITNESS_SMTP_PASSWORD": "abcd efgh ijkl mnop"}
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_missing_password_names_the_variable_to_set(monkeypatch):
    # This runs from launchd, where a log line is the only diagnostic.
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_USER", "me@gmail.com")
    with pytest.raises(mailer.MailNotConfigured, match="LOCAL_FITNESS_SMTP_PASSWORD"):
        mailer.load_config()


def test_missing_user_names_the_variable_to_set(monkeypatch):
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_PASSWORD", "pw")
    with pytest.raises(mailer.MailNotConfigured, match="LOCAL_FITNESS_SMTP_USER"):
        mailer.load_config()


def test_a_blank_password_line_reads_as_missing_not_as_an_empty_password(monkeypatch):
    # `LOCAL_FITNESS_SMTP_PASSWORD=` is the shape a half-finished .env takes.
    # It must produce the actionable error, not an opaque SMTP auth rejection.
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("LOCAL_FITNESS_SMTP_PASSWORD", "   ")
    with pytest.raises(mailer.MailNotConfigured, match="LOCAL_FITNESS_SMTP_PASSWORD"):
        mailer.load_config()


def test_app_password_spaces_are_stripped(monkeypatch):
    # Google displays the 16-char password as four quads and it gets pasted
    # that way.
    _configured(monkeypatch)
    assert mailer.load_config().password == "abcdefghijklmnop"


def test_gmail_defaults_apply_when_host_and_port_are_unset(monkeypatch):
    _configured(monkeypatch)
    cfg = mailer.load_config()
    assert (cfg.host, cfg.port) == ("smtp.gmail.com", 465)


def test_host_and_port_are_overridable_for_another_provider(monkeypatch):
    _configured(monkeypatch, LOCAL_FITNESS_SMTP_HOST="smtp.fastmail.com",
                LOCAL_FITNESS_SMTP_PORT="587")
    cfg = mailer.load_config()
    assert (cfg.host, cfg.port) == ("smtp.fastmail.com", 587)


def test_recipient_defaults_to_the_sending_account(monkeypatch):
    _configured(monkeypatch)
    assert mailer.load_config().to == ("me@gmail.com",)


def test_multiple_recipients_are_split_and_trimmed(monkeypatch):
    _configured(monkeypatch, LOCAL_FITNESS_BRIEF_EMAIL_TO="a@x.com, b@x.com ,")
    assert mailer.load_config().to == ("a@x.com", "b@x.com")


def test_to_override_beats_the_environment(monkeypatch):
    _configured(monkeypatch, LOCAL_FITNESS_BRIEF_EMAIL_TO="env@x.com")
    assert mailer.load_config("cli@x.com").to == ("cli@x.com",)


def test_a_non_numeric_port_fails_with_a_readable_message(monkeypatch):
    _configured(monkeypatch, LOCAL_FITNESS_SMTP_PORT="four-six-five")
    with pytest.raises(mailer.MailNotConfigured, match="must be a number"):
        mailer.load_config()


def test_from_defaults_to_user_but_is_overridable(monkeypatch):
    _configured(monkeypatch)
    assert mailer.load_config().from_addr == "me@gmail.com"
    monkeypatch.setenv("LOCAL_FITNESS_BRIEF_EMAIL_FROM", "coach@x.com")
    assert mailer.load_config().from_addr == "coach@x.com"


def test_placeholder_config_carries_no_credential():
    # --dry-run exists to be usable BEFORE a password is in place; a
    # placeholder that leaked a real one would defeat the point.
    cfg = mailer.placeholder_config()
    assert cfg.password == ""
    assert cfg.to and cfg.from_addr


# --- transport selection ---------------------------------------------------

class _FakeSMTP:
    """Records what the transport did without opening a socket."""
    instances: list[_FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def test_port_465_uses_implicit_tls_without_starttls(fake_smtp):
    msg = build({})
    mailer.send(msg, CFG)
    sent = fake_smtp.instances[0]
    assert (sent.host, sent.port) == ("smtp.example.com", 465)
    assert sent.started_tls is False
    assert sent.logged_in == ("me@example.com", "secret")
    assert sent.sent is msg


def test_port_587_upgrades_with_starttls_before_authenticating(fake_smtp):
    # Credentials must never cross an unupgraded socket.
    cfg = mailer.MailConfig(**{**CFG.__dict__, "port": 587})
    mailer.send(build({}), cfg)
    assert fake_smtp.instances[0].started_tls is True
    assert fake_smtp.instances[0].logged_in == ("me@example.com", "secret")


def test_a_custom_port_also_upgrades(fake_smtp):
    cfg = mailer.MailConfig(**{**CFG.__dict__, "port": 2525})
    mailer.send(build({}), cfg)
    assert fake_smtp.instances[0].started_tls is True


def test_the_socket_carries_a_timeout(fake_smtp):
    # Unattended behind a launchd backstop: a hung socket must fail and free
    # the retry slot rather than pin the process.
    mailer.send(build({}), CFG)
    assert fake_smtp.instances[0].timeout == mailer.SMTP_TIMEOUT_S
