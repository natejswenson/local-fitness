"""SMTP delivery for the evening brief.

The persistence/IO half of the email path — ``email_render`` is the pure half,
same divider as ``plans.py`` and ``journal.py``.

Deliberately provider-agnostic with Gmail's values as the defaults: the
deployment this was built for sends through Gmail, but every knob is an env
var so a fresh clone can point at any SMTP host without editing tracked code
(the env-driven pattern in CLAUDE.md). The one value with no default is the
password — a secret must never fall back to anything.

No Claude, no MCP, no model turn anywhere in this path. That is the whole
reason the charts survive at full fidelity: attachment bytes go from
matplotlib into the MIME tree directly, instead of being retyped as base64
into a tool call.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .. import config
from .email_render import chart_cid

LOG = logging.getLogger(__name__)

DEFAULT_SMTP_HOST = "smtp.gmail.com"
#: 465 = implicit TLS (SMTPS). 587 is handled too, via STARTTLS — see `send`.
DEFAULT_SMTP_PORT = 465

#: How long to wait on the SMTP conversation. The job runs unattended behind a
#: launchd backstop, so a hung socket must fail and let the retry slot take it
#: rather than pin the process until launchd's own timeout.
SMTP_TIMEOUT_S = 30


class MailNotConfigured(RuntimeError):
    """Raised when required mail settings are missing.

    Distinct from a send failure on purpose: this one is actionable by the
    user (put a value in ``.env``) and is never worth retrying, so the CLI
    reports it differently from a transient SMTP error.
    """


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    user: str
    password: str
    to: tuple[str, ...]
    from_addr: str


def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var, treating whitespace-only as unset.

    An empty ``LOCAL_FITNESS_SMTP_PASSWORD=`` line in ``.env`` is the shape a
    half-finished setup actually takes — it must read as missing (a clear
    MailNotConfigured) rather than as an empty password (an opaque SMTP auth
    rejection at 19:00).
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def load_config(to_override: str | None = None) -> MailConfig:
    """Resolve mail settings from the environment.

    Raises ``MailNotConfigured`` naming the missing variable — this runs from
    launchd where the only diagnostic is a log line, so the error has to say
    exactly which value to set.
    """
    user = _env("LOCAL_FITNESS_SMTP_USER")
    # Gmail app passwords are displayed as four space-separated quads and get
    # pasted that way; Gmail accepts either form, but stripping is what makes
    # the pasted-with-spaces case work everywhere.
    password = _env("LOCAL_FITNESS_SMTP_PASSWORD")
    password = password.replace(" ", "") if password else None

    if not password:
        raise MailNotConfigured(
            "LOCAL_FITNESS_SMTP_PASSWORD is not set. Put a Gmail app password "
            "(myaccount.google.com/apppasswords) in <repo>/.env — never in "
            ".env.example, which is tracked."
        )
    if not user:
        raise MailNotConfigured(
            "LOCAL_FITNESS_SMTP_USER is not set (the account that sends the "
            "mail, e.g. you@gmail.com). Add it to <repo>/.env."
        )

    # Recipients resolve DB > env > the sending account, so the MCP tools can
    # change them conversationally. `config` owns the first two layers; the
    # fallback to `user` lives here because config deliberately holds no SMTP
    # state. An explicit --to beats all of it.
    if to_override:
        recipients = tuple(a.strip() for a in to_override.split(",") if a.strip())
    else:
        recipients = config.brief_email_to() or (user,)
    if not recipients:
        raise MailNotConfigured(
            "No recipients resolved. Set one with the "
            "update_brief_email_settings MCP tool, or "
            "LOCAL_FITNESS_BRIEF_EMAIL_TO in .env.")

    port_raw = _env("LOCAL_FITNESS_SMTP_PORT") or str(DEFAULT_SMTP_PORT)
    try:
        port = int(port_raw)
    except ValueError as e:
        raise MailNotConfigured(
            f"LOCAL_FITNESS_SMTP_PORT must be a number, got {port_raw!r}") from e

    return MailConfig(
        host=_env("LOCAL_FITNESS_SMTP_HOST") or DEFAULT_SMTP_HOST,
        port=port,
        user=user,
        password=password,
        to=recipients,
        from_addr=_env("LOCAL_FITNESS_BRIEF_EMAIL_FROM") or user,
    )


def password_configured() -> bool:
    """Whether a sending credential is present — WITHOUT exposing it.

    This is the only thing the MCP surface may learn about the password. The
    value itself is deliberately unreachable from any tool: `/mcp/` is served
    over the network and reachable from a phone, so a settings tool that
    echoed the credential would publish a live Gmail app password to every
    client that can call it. "Is it set?" answers the only question a
    configuration reader legitimately has.
    """
    raw = _env("LOCAL_FITNESS_SMTP_PASSWORD")
    return bool(raw and raw.replace(" ", ""))


def placeholder_config() -> MailConfig:
    """A config with no real credentials, for ``--dry-run``.

    Dry-run exists to check the rendered message *before* a password is in
    place — which is exactly when ``load_config`` refuses. Building the MIME
    tree needs From/To headers and nothing else, so those are the only fields
    that carry a visible value; the password is empty because nothing in the
    dry-run path opens a socket.
    """
    return MailConfig(
        host=DEFAULT_SMTP_HOST,
        port=DEFAULT_SMTP_PORT,
        user="dry-run@localhost",
        password="",
        to=("dry-run@localhost",),
        from_addr="dry-run@localhost",
    )


def build_message(
    subject: str,
    html_body: str,
    text_body: str,
    charts: dict[str, bytes],
    cfg: MailConfig,
) -> EmailMessage:
    """Assemble the MIME tree.

    Structure is ``multipart/alternative`` → [``text/plain``,
    ``multipart/related`` → [``text/html``, ``image/png``...]]. That nesting is
    what makes a ``cid:`` reference resolve: the images must be *related* to
    the HTML part specifically, not siblings of the whole message, or clients
    show them as loose downloads instead of inline figures.

    ``charts`` is keyed by ``str(index)`` over ``enumerate(brief.takeaways)`` —
    the same keying ``generate_brief_report`` uses, and the same keying
    ``email_render.chart_cid`` turns into a content-id, so the ``src="cid:…"``
    written by the renderer and the ``Content-ID`` header written here cannot
    drift apart.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.to)
    msg["Date"] = formatdate(localtime=True)
    # An explicit Message-ID keyed to the sending domain; without one some
    # relays synthesize a low-reputation value.
    msg["Message-ID"] = make_msgid(domain=cfg.from_addr.split("@")[-1] or None)
    # Self-sent daily mail is the classic false-positive for bulk filters.
    # Naming the generator is cheap provenance if one ever gets misrouted.
    msg["X-Mailer"] = "local-fitness"

    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    # payload[1] is the text/html part just added; add_related must be called
    # on IT, not on `msg`, or the images attach one level too high.
    html_part = msg.get_payload()[-1]
    for key in sorted(charts, key=lambda k: int(k)):
        html_part.add_related(
            charts[key],
            maintype="image",
            subtype="png",
            # add_related stores the value as-is, and RFC 2392 requires the
            # angle brackets in the header while the `cid:` URL omits them.
            cid=f"<{chart_cid(key)}>",
            filename=f"{chart_cid(key)}.png",
        )
    return msg


def tls_context() -> ssl.SSLContext:
    """A CERTIFICATE-VERIFYING TLS context. Never omit this.

    `smtplib` does NOT verify by default, on either transport. Both
    `SMTP_SSL.__init__` and `SMTP.starttls()` fall back to
    `ssl._create_stdlib_context()` when handed `context=None`, and that factory
    returns `check_hostname=False, verify_mode=CERT_NONE` — encrypted but
    UNAUTHENTICATED, accepting any certificate from any peer. `create_default_
    context()` is the one that returns `check_hostname=True,
    verify_mode=CERT_REQUIRED`.

    This shipped wrong in 0.51.0 and was caught by the pre-release security
    review. It matters more here than in an interactive client: the send runs
    unattended from launchd at 19:00 with a 20:00 retry, so there is no human
    to notice a bad certificate, and the very next statement after the
    handshake is `login()` — which puts a Gmail app password on the wire in
    AUTH PLAIN. An attacker with a network position (LAN ARP spoofing, a
    hostile router, DNS poisoning) would have received the credential to the
    user's entire mailbox, nightly, plus the health data in the brief.

    Exposed as a function rather than inlined so the tests can assert on the
    context's actual `check_hostname` / `verify_mode` — a test that only
    asserts "it sent" cannot catch this, which is exactly how it survived
    review the first time.
    """
    return ssl.create_default_context()


def send(msg: EmailMessage, cfg: MailConfig) -> None:
    """Deliver via SMTP. Raises on failure — the caller decides what a failed
    send means (the CLI turns it into a macOS notification and a non-zero
    exit, so a silent evening is never the only signal)."""
    context = tls_context()
    if cfg.port == 465:
        # Implicit TLS: the context must be supplied at CONNECT time, since
        # the handshake happens inside __init__.
        with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_S,
                              context=context) as smtp:
            smtp.login(cfg.user, cfg.password)
            smtp.send_message(msg)
    else:
        # 587 and anything custom: connect in the clear, then upgrade. Sending
        # credentials over an unupgraded socket is not an option, so a host
        # that refuses STARTTLS fails loudly here — and the upgrade itself must
        # carry the verifying context or the "upgrade" authenticates nothing.
        with smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.starttls(context=context)
            smtp.login(cfg.user, cfg.password)
            smtp.send_message(msg)
    LOG.info("brief_email sent to=%s subject=%r", ", ".join(cfg.to), msg["Subject"])
