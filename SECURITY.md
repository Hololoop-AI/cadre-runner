# Security

## What Cadre trusts

Cadre runs coding agents on your machine with permission prompts turned off,
and its fleet page has **no login**: anyone who can reach the page can start
and steer those agents. So:

- The page listens on `127.0.0.1` only, unless `status_bind` in
  `config.local.toml` adds another address.
- Add only addresses on a private network you control (a VPN such as
  Tailscale, or a trusted LAN). Never bind `0.0.0.0` on a network you share,
  and never put the page on the public internet.
- Agents act with your user's permissions. Run Cadre as a user whose files and
  credentials you are willing to let an agent use.

## Reporting a vulnerability

Please report it privately through GitHub: on this repository's **Security**
tab, choose **Report a vulnerability**. Do not open a public issue for it.
We aim to acknowledge a report within a week.

## Supported versions

Only the latest release (see `VERSION` and `CHANGELOG.md`) gets fixes.
