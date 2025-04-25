FROM registry.suse.com/bci/python:3.13

WORKDIR /app
COPY . .
RUN zypper --non-interactive addrepo --no-gpgcheck https://download.opensuse.org/repositories/SUSE:/CA/15.6/ SUSE_CA \
    && zypper --gpg-auto-import-keys refresh \
    && zypper --non-interactive in -y ca-certificates-suse \
    && zypper clean --all

RUN groupadd --gid 1000 app && \
    useradd -m --uid 1000 --gid app --shell /bin/bash app && \
    chown -R app:app /app
USER app
ENV PATH="$PATH:/home/app/.local/bin"
RUN pipx install poetry && \
    poetry install
CMD ["poetry", "run", "python", "slacky/main.py"]

