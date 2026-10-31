# SuperAIMail

- Standalone, single-user mail client. Python standard library backend in `superaimail/engine`; browser UI in `superaimail/web`; desktop and Android wrappers share both.
- Never copy the original operations workspace's runtime data, credentials, private endpoints or user records into this repository. Use synthetic fixtures only.
- Preserve passwordless AI-off mail receiving. AI enhancement must be opt-in and must not block indexing or send mail automatically.
- Mailbox read/star/archive/delete flags are local; IMAP remains read-only. Sending requires a preview and explicit confirmation.
- Run `python -m unittest discover -s tests -v`, `python -m compileall -q superaimail`, and `node --check superaimail/web/app.js` after affected changes. Tests use temporary data and isolated protocol servers.
- Keep source, tutorials and install packages consistent. Report desktop, browser, Android build and actual-device evidence separately.
- Commit verified changes with Conventional Commits. Publish or push only within the user's requested scope.
