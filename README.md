# Offline Archiver for Windows

A desktop application for archiving web content that the signed-in user is authorized to access. It preserves a persistent browser profile for normal interactive login, crawls configured same-site links, stores snapshots and assets locally, and provides offline browsing/search/export.

## Safety and scope
- Use only sites and content you are authorized to access and save.
- The application does not bypass paywalls, DRM, MFA, CAPTCHAs, or access controls.
- Sign in manually in the visible browser window. MFA and provider challenges remain under the user's control.
- Respect site terms, robots directives where applicable, and configured crawl limits.

## Development
- Windows 10/11, Python 3.11+
- Install: `py -3.11 -m venv .venv`, `.venv\\Scripts\\activate`, `pip install -r requirements.txt`, `playwright install msedge`
- Run: `python -m offline_archiver`

The Windows executable is built by GitHub Actions on pushes to main and on version tags. Build artifacts are test packages, not a claim of production certification. Validate with your own authorized accounts and target sites before relying on archives.

## Storage
The library is stored under the user's LocalAppData directory by default. Browser profiles and cookies are local and must not be shared. Archive exports contain captured content and should be treated as sensitive.
