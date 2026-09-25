"""Server-rendered /login page.

No React, no JavaScript dependency. Listed providers come from the
registry; clicking a provider sends a GET to
``/auth/login?provider=<name>``.

Visual styling mirrors the Nous Research design system (the
``@nous-research/ui`` package the React dashboard uses): the same
``Collapse`` / ``Rules Compressed`` typeface, amber-on-dark colour
tokens (``#170d02`` / ``#ffac02`` / ``#fff``), uppercase + wide-tracking
brand chrome, and the inset-bevel button shadow. Fonts are served
out of the SPA's ``/fonts/`` directory which the dashboard-auth gate
already allowlists pre-auth (see ``_GATE_PUBLIC_PREFIXES`` in
``middleware.py``), so the page renders without needing the React
bundle loaded.

Test-stable class names: the existing test suite extracts the
``class="provider-btn"`` anchor href to walk the OAuth flow. That
class name MUST NOT change without updating
``tests/hermes_cli/test_dashboard_auth_401_reauth.py``.
"""
from __future__ import annotations

import html

from hermes_cli.dashboard_auth import list_session_providers

# Inline minimal CSS. The dashboard's full skin lives in the React
# bundle, which we deliberately do NOT load here — the login page must
# not depend on the SPA build being present or on the injected session
# token.
#
# Single curly braces are placeholders for ``str.format``; CSS curlies
# are doubled (``{{`` / ``}}``).
_LOGIN_HTML_TEMPLATE = """\
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — Hermes Up Great</title>
<style>
  /* Brand fonts shipped by @nous-research/ui — same files the SPA loads. */
  @font-face {{
    font-family: 'Collapse';
    font-style: normal;
    font-weight: 400;
    font-display: swap;
    src: url('/fonts/Collapse-Regular.woff2') format('woff2');
  }}
  @font-face {{
    font-family: 'Collapse';
    font-style: normal;
    font-weight: 700;
    font-display: swap;
    src: url('/fonts/Collapse-Bold.woff2') format('woff2');
  }}
  @font-face {{
    font-family: 'Rules Compressed';
    font-style: normal;
    font-weight: 400;
    font-display: swap;
    src: url('/fonts/RulesCompressed-Regular.woff2') format('woff2');
  }}
  @font-face {{
    font-family: 'Rules Compressed';
    font-style: normal;
    font-weight: 600;
    font-display: swap;
    src: url('/fonts/RulesCompressed-Medium.woff2') format('woff2');
  }}

  :root {{
    --background-base: #170d02;
    --background: #170d02;
    --midground: #ffac02;
    --foreground: #ffffff;
    --hairline: color-mix(in srgb, #ffac02 18%, transparent);
    --hairline-strong: color-mix(in srgb, #ffac02 35%, transparent);
  }}

  *, *::before, *::after {{ box-sizing: border-box; }}

  html, body {{
    margin: 0;
    padding: 0;
    min-height: 100%;
    background: var(--background-base);
    color: var(--foreground);
    font-family: 'Collapse', system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 16px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }}

  /* Subtle dot-grid backdrop — DS idiom (see `.dither` in globals.css). */
  body {{
    background-image:
      radial-gradient(
        ellipse at top,
        color-mix(in srgb, var(--midground) 6%, transparent) 0%,
        transparent 55%
      ),
      repeating-conic-gradient(
        color-mix(in srgb, var(--midground) 4%, transparent) 0% 25%,
        transparent 0% 50%
      );
    background-size: auto, 3px 3px;
    background-attachment: fixed;
  }}

  /* Layout: vertically center on tall screens, top-anchor on short. */
  body {{
    display: grid;
    place-items: center;
    padding: clamp(1.5rem, 6vh, 6rem) 1.25rem;
  }}

  main {{
    width: 100%;
    max-width: 26rem;
    position: relative;
    animation: slide-up 0.6s ease-out both;
  }}

  @keyframes slide-up {{
    from {{ opacity: 0; transform: translateY(6px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}

  @media (prefers-reduced-motion: reduce) {{
    main {{ animation: none; }}
  }}

  /* Brand wordmark above the card — same uppercase + wide-tracking
     idiom DS Buttons use. */
  .brand {{
    text-align: center;
    margin-bottom: 1.75rem;
    font-family: 'Rules Compressed', 'Collapse', sans-serif;
    font-weight: 600;
    font-size: 1.05rem;
    letter-spacing: 0.32em;
    text-transform: uppercase;
    color: var(--midground);
  }}
  .brand .dot {{
    display: inline-block;
    width: 6px;
    height: 6px;
    background: var(--midground);
    margin: 0 0.55em 0.18em;
    vertical-align: middle;
    border-radius: 1px;
  }}

  .card {{
    position: relative;
    padding: 2.25rem 2rem 2rem;
    background: color-mix(in srgb, #ffffff 2%, var(--background-base));
    border: 1px solid var(--hairline);
    /* Hairline highlight + bevel shadow — matches DS Button SHADOW_DEFAULT
       (`inset -1px -1px 0 #00000080, inset 1px 1px 0 #ffffff80`) at panel scale. */
    box-shadow:
      inset 1px 1px 0 0 color-mix(in srgb, #ffffff 5%, transparent),
      inset -1px -1px 0 0 rgba(0, 0, 0, 0.4),
      0 24px 60px -20px rgba(0, 0, 0, 0.6);
  }}

  h1 {{
    margin: 0 0 0.4rem;
    font-family: 'Rules Compressed', 'Collapse', sans-serif;
    font-weight: 600;
    font-size: 1.85rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--foreground);
  }}

  .subtitle {{
    margin: 0 0 1.75rem;
    color: color-mix(in srgb, var(--foreground) 65%, transparent);
    font-size: 0.95rem;
  }}

  .provider-list {{
    display: grid;
    gap: 0.75rem;
  }}

  /* Provider button — mirrors DS Button (default variant):
     amber surface, dark text, uppercase + wide tracking, inset bevel. */
  .provider-btn {{
    display: block;
    width: 100%;
    box-sizing: border-box;
    padding: 0.95rem 1rem;
    text-align: center;
    background: var(--midground);
    color: var(--background-base);
    font-family: 'Collapse', sans-serif;
    font-weight: 700;
    font-size: 0.78rem;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    text-decoration: none;
    border: 0;
    border-radius: 0;  /* DS Button is squared — no rounded corners. */
    cursor: pointer;
    box-shadow:
      inset 1px 1px 0 0 rgba(255, 255, 255, 0.5),
      inset -1px -1px 0 0 rgba(0, 0, 0, 0.5);
    transition: filter 0.12s ease-out;
  }}
  .provider-btn:hover {{
    filter: brightness(1.08);
  }}
  .provider-btn:active {{
    /* DS Button uses `active:invert` on the default surface. */
    filter: invert(1);
  }}
  .provider-btn:focus-visible {{
    outline: 2px solid var(--midground);
    outline-offset: 3px;
  }}

  /* Password provider form — same visual language as the OAuth buttons:
     squared inputs, hairline borders, amber focus ring. */
  .provider-form {{
    display: grid;
    gap: 0.75rem;
    text-align: left;
  }}
  .form-title {{
    font-family: 'Rules Compressed', 'Collapse', sans-serif;
    font-weight: 600;
    font-size: 0.72rem;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: color-mix(in srgb, var(--foreground) 70%, transparent);
  }}
  .field {{
    display: grid;
    gap: 0.3rem;
  }}
  .field-label {{
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: color-mix(in srgb, var(--foreground) 55%, transparent);
  }}
  .field-input {{
    width: 100%;
    box-sizing: border-box;
    padding: 0.7rem 0.8rem;
    background: color-mix(in srgb, #000000 25%, var(--background-base));
    color: var(--foreground);
    border: 1px solid var(--hairline-strong);
    border-radius: 0;
    font-family: 'Collapse', sans-serif;
    font-size: 0.95rem;
  }}
  .field-input:focus-visible {{
    outline: none;
    border-color: var(--midground);
    box-shadow: 0 0 0 1px var(--midground);
  }}
  .form-error {{
    color: #ff6b6b;
    font-size: 0.82rem;
    letter-spacing: 0.02em;
  }}
  .provider-form .provider-btn {{
    margin-top: 0.25rem;
  }}

  footer {{
    margin-top: 1.75rem;
    text-align: center;
    color: color-mix(in srgb, var(--foreground) 45%, transparent);
    font-size: 0.75rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    line-height: 1.7;
  }}
  footer .sep {{
    display: inline-block;
    width: 1.5rem;
    height: 1px;
    background: var(--hairline-strong);
    vertical-align: middle;
    margin: 0 0.6em 0.2em;
  }}

  /* Selection — DS uses midground bg + background text. */
  ::selection {{
    background: var(--midground);
    color: var(--background-base);
  }}
  /* Up Great dashboard identity — official palette and accessible contrast. */
  :root {{ --background-base: #F4F8F9; --background: #F4F8F9; --midground: #20CBD4; --foreground: #FFFFFF; --hairline: rgba(0,123,135,.32); --hairline-strong: rgba(0,123,135,.58); }}
  html, body {{ background-color: #F4F8F9; color: #253746; font-family: Arial, Helvetica, sans-serif; }}
  body {{ background-image: radial-gradient(ellipse at top, rgba(32,203,212,.16) 0%, transparent 55%), repeating-conic-gradient(rgba(0,123,135,.035) 0% 25%, transparent 0% 50%); }}
  .brand {{ line-height: 0; }} .brand .dot {{ display: none; }} .brand svg {{ width: 138px; height: auto; }}
  .card {{ background: #253746; border-color: rgba(32,203,212,.7); box-shadow: 0 24px 60px -20px rgba(37,55,70,.45); }}
  h1 {{ color: #FFFFFF; font-family: Arial, Helvetica, sans-serif; letter-spacing: .02em; text-transform: none; }}
  .subtitle {{ color: #C2C6C9; }} .form-title {{ color: #FFFFFF; }} .field-label {{ color: #C2C6C9; }}
  .field-input {{ background: #FFFFFF; color: #253746; font-family: Arial, Helvetica, sans-serif; }}
  .provider-btn {{ color: #253746; font-family: Arial, Helvetica, sans-serif; }} footer {{ color: #60727B; }}
</style>
</head>
<body>
<main>
  <div class="brand" aria-label="Технологические конкурсы Up Great"><svg width="92" height="34" viewBox="0 0 92 34" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Up Great"><g clip-path="url(#upgreat-clip)"><path fill-rule="evenodd" clip-rule="evenodd" d="M0 9.22841V0H3.5213V9.11315C3.5213 10.7987 4.1998 12.7275 6.71945 12.7275C9.23795 12.7275 9.89287 10.7987 9.89287 9.11315V0H13.4389V9.20593C13.4389 12.3904 11.6828 15.6267 6.71945 15.6267C1.75547 15.6267 0 12.2458 0 9.22841ZM15.429 15.3501V0H24.5508C27.8622 0 29.6671 2.25553 29.6671 4.94844C29.6671 7.61773 27.8398 9.89573 24.5508 9.89573H19.3372V15.3501H15.429ZM26.0222 4.94844C26.0222 3.65932 25.0631 2.87675 23.7837 2.87675H19.3372V7.01898H23.7837C25.0631 7.01898 26.0222 6.21336 26.0222 4.94844Z" fill="white"/><path fill-rule="evenodd" clip-rule="evenodd" d="M77.231 16.9712V20.2709H82.4882V32.7944H86.7185V20.2709H91.9522V16.9712H77.231ZM66.4843 17.2778L60.5905 32.6279H65.0571L65.7241 30.819L72.8311 30.8132L73.4843 32.6279H77.8492L75.7544 27.1487H71.6564L71.657 27.1504H66.9063L69.2822 20.5452L71.2913 26.1327H75.3531L71.9795 17.2778H66.4843ZM45.1449 17.2778V32.6279H58.4538V29.1933H49.0532V26.5062H57.9616V23.0399H49.0532V20.2473H58.6148V17.2778H45.1449ZM28.4066 17.2778V32.6285H32.3155V27.8437H35.4147L38.4064 32.6285H42.7948L39.3696 27.4536C41.0135 27.0611 42.873 25.1323 42.873 22.417C42.873 19.5638 40.7628 17.2783 37.5876 17.2783H28.406L28.4066 17.2778ZM32.3155 20.1545H37.1081C38.4099 20.1545 39.392 20.9371 39.392 22.2256C39.392 23.5142 38.4099 24.2967 ... [truncated]
  <div class="card">
    <h1>Вход в Hermes</h1>
    <p class="subtitle">Войдите, чтобы продолжить работу в системе Hermes для технологических конкурсов Up Great.</p>
    <div class="provider-list">
{provider_buttons}
    </div>
  </div>
  <footer>
    <span class="sep"></span>Защищённый доступ<span class="sep"></span>
  </footer>
</main>
{password_script}
</body>
</html>
"""

_EMPTY_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign-in unavailable — Hermes Agent</title>
<style>
  @font-face {
    font-family: 'Collapse';
    font-style: normal;
    font-weight: 400;
    font-display: swap;
    src: url('/fonts/Collapse-Regular.woff2') format('woff2');
  }
  @font-face {
    font-family: 'Rules Compressed';
    font-style: normal;
    font-weight: 600;
    font-display: swap;
    src: url('/fonts/RulesCompressed-Medium.woff2') format('woff2');
  }
  :root {
    --background-base: #170d02;
    --midground: #ffac02;
    --foreground: #ffffff;
    --hairline: color-mix(in srgb, #ffac02 18%, transparent);
  }
  *, *::before, *::after { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; min-height: 100%;
    background: var(--background-base);
    color: var(--foreground);
    font-family: 'Collapse', system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 16px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  body {
    display: grid; place-items: center;
    padding: clamp(1.5rem, 6vh, 6rem) 1.25rem;
  }
  main {
    width: 100%; max-width: 32rem;
    padding: 2.25rem 2rem;
    background: color-mix(in srgb, #ffffff 2%, var(--background-base));
    border: 1px solid var(--hairline);
    box-shadow:
      inset 1px 1px 0 0 color-mix(in srgb, #ffffff 5%, transparent),
      inset -1px -1px 0 0 rgba(0, 0, 0, 0.4),
      0 24px 60px -20px rgba(0, 0, 0, 0.6);
  }
  h1 {
    margin: 0 0 1rem;
    font-family: 'Rules Compressed', 'Collapse', sans-serif;
    font-weight: 600; font-size: 1.5rem;
    letter-spacing: 0.05em; text-transform: uppercase;
    color: var(--midground);
  }
  p { margin: 0 0 1rem; }
  code {
    background: var(--midground);
    color: var(--background-base);
    padding: 0.1em 0.35em;
    font-family: 'Courier New', monospace;
    font-size: 0.9em;
  }
</style>
</head>
<body>
<main>
<h1>Sign-in unavailable</h1>
<p>This dashboard is bound to a non-loopback host but no authentication
providers are installed.</p>
<p>Install <code>plugins/dashboard-auth-nous</code> (default) or another
auth provider, or restart with <code>--insecure</code> to bypass the
auth gate (not recommended on untrusted networks).</p>
</main>
</body>
</html>
"""


# Inline script that wires every password provider form to POST JSON to
# ``/auth/password-login`` and navigate on success. Emitted ONLY when at
# least one ``supports_password`` provider is listed (OAuth-only login
# pages stay script-free, preserving the no-JS contract for that case).
#
# Plain string (NOT run through ``str.format``), so braces are literal —
# do not double them. A single delegated submit handler covers all forms;
# the provider name is read from the form's ``data-provider`` attribute.
_PASSWORD_FORM_SCRIPT = """\
<script>
(function () {
  // Derive the proxy prefix from the rendered login URL. Direct deploys use
  // /login (basePath=""); prefixed deploys use /dashboard/login.
  var loginPath = window.location.pathname || '/login';
  var basePath = loginPath.endsWith('/login')
    ? loginPath.slice(0, -'/login'.length)
    : '';
  function handle(form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var err = form.querySelector('.form-error');
      var btn = form.querySelector('button[type=submit]');
      if (err) { err.hidden = true; err.textContent = ''; }
      if (btn) { btn.disabled = true; }
      var body = {
        provider: form.getAttribute('data-provider') || '',
        username: (form.querySelector('input[name=username]') || {}).value || '',
        password: (form.querySelector('input[name=password]') || {}).value || '',
        next: (form.querySelector('input[name=next]') || {}).value || ''
      };
      fetch(basePath + '/auth/password-login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        credentials: 'same-origin'
      }).then(function (resp) {
        if (resp.ok) {
          return resp.json().then(function (data) {
            var destination = (data && data.next) || '/';
            if (basePath && destination.charAt(0) === '/' &&
                destination !== basePath && !destination.startsWith(basePath + '/')) {
              destination = basePath + destination;
            }
            window.location.assign(destination);
          });
        }
        var msg = resp.status === 429
          ? 'Too many attempts. Please wait and try again.'
          : (resp.status === 401 ? 'Invalid username or password.'
                                 : 'Sign-in failed. Please try again.');
        if (err) { err.textContent = msg; err.hidden = false; }
        if (btn) { btn.disabled = false; }
      }).catch(function () {
        if (err) { err.textContent = 'Network error. Please try again.'; err.hidden = false; }
        if (btn) { btn.disabled = false; }
      });
    });
  }
  var forms = document.querySelectorAll('form.provider-form');
  for (var i = 0; i < forms.length; i++) { handle(forms[i]); }
})();
</script>
"""


def render_login_html(*, next_path: str = "") -> str:
    """Return the full HTML for ``GET /login``.

    ``next_path`` — when set, the post-login landing path the user
    originally requested. Threaded into each provider button's ``href``
    as a ``next=`` query parameter so the OAuth round trip carries it
    end-to-end. The caller (``routes.login_page``) is responsible for
    validating ``next_path`` against the same-origin rules before we
    emit it; we still HTML-escape it as defence in depth.
    """
    providers = list_session_providers()
    if not providers:
        return _EMPTY_HTML

    if next_path:
        # URL-encode then HTML-escape. The URL-encode step matches the
        # gate's ``_safe_next_target`` output shape (also URL-encoded),
        # so a value that round-tripped from /login?next=... back into
        # the button href is byte-identical.
        from urllib.parse import quote
        next_qs = f"&next={html.escape(quote(next_path, safe=''), quote=True)}"
    else:
        next_qs = ""

    buttons = []
    needs_password_script = False
    for p in providers:
        if getattr(p, "supports_password", False):
            needs_password_script = True
            buttons.append(_render_password_form(p, next_path))
        else:
            buttons.append(
                f'      <a class="provider-btn" '
                f'href="/auth/login?provider={html.escape(p.name, quote=True)}{next_qs}">'
                f'Sign in with {html.escape(p.display_name)}</a>'
            )
    script = _PASSWORD_FORM_SCRIPT if needs_password_script else ""
    return _LOGIN_HTML_TEMPLATE.format(
        provider_buttons="\n".join(buttons),
        password_script=script,
    )


def _render_password_form(provider, next_path: str) -> str:
    """Render a username/password form for a ``supports_password`` provider.

    The form is wired by :data:`_PASSWORD_FORM_SCRIPT` (a single delegated
    submit handler) to POST JSON to ``/auth/password-login`` and navigate
    on success. ``next_path`` is carried in a hidden field; it has already
    been validated same-origin by the caller and is HTML-escaped here as
    defence in depth. The provider ``name`` is emitted in a ``data-``
    attribute (not a hidden input) so the script reads it without trusting
    form-field ordering.
    """
    pname = html.escape(provider.name, quote=True)
    plabel = html.escape(provider.display_name)
    safe_next = html.escape(next_path, quote=True) if next_path else ""
    return (
        f'      <form class="provider-form" data-provider="{pname}" '
        f'autocomplete="on">\n'
        f'        <div class="form-title">Sign in with {plabel}</div>\n'
        f'        <input type="hidden" name="next" value="{safe_next}">\n'
        f'        <label class="field">\n'
        f'          <span class="field-label">Username</span>\n'
        f'          <input class="field-input" type="text" name="username" '
        f'autocomplete="username" autocapitalize="none" '
        f'autocorrect="off" spellcheck="false" required>\n'
        f'        </label>\n'
        f'        <label class="field">\n'
        f'          <span class="field-label">Password</span>\n'
        f'          <input class="field-input" type="password" name="password" '
        f'autocomplete="current-password" required>\n'
        f'        </label>\n'
        f'        <div class="form-error" role="alert" hidden></div>\n'
        f'        <button class="provider-btn" type="submit">Sign in</button>\n'
        f'      </form>'
    )

