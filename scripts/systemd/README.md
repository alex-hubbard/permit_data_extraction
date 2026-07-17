# systemd user units for long-running jobs

Installed copies live in `~/.config/systemd/user/`. These are the
version-controlled sources; after editing, re-copy and reload:

    cp scripts/systemd/*.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now freemodel-reextract la-edms-download

Requires `loginctl enable-linger` (already on for afhubbard) so units start
at WSL VM boot without a login. Both jobs are resumable, so restarts are
safe. `la-edms-download` exits non-zero while any doc errors remain and will
re-poll every 15 min — disable it once the download is done:

    systemctl --user disable --now la-edms-download
