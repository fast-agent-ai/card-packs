# Edit Assistant

Adds:

- `/peek <message>` for one-off questions.
- `/edit-last` (`Ctrl+X E`) for editing the last assistant response before
  sending a revised reply.
- `/annotate-last` (`Ctrl+X A`) for reviewing the last assistant response in
  [AnnotUI](https://github.com/dutifuldev/annotui) and prefilling its Markdown
  annotations as the next user message.

`annotate-last` runs `annotui` from `$PATH`. Set `$ANNOTUI` to override the
command, including optional arguments such as `annotui --no-mouse`.
