FROM registry.suse.com/bci/python:3.13

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
RUN zypper addrepo https://download.opensuse.org/repositories/SUSE:/CA/openSUSE_Tumbleweed/ SUSE_CA 
RUN zypper --gpg-auto-import-keys refresh 
RUN zypper in -y  ca-certificates-suse

CMD [ "python3", "./slacky.py" ]
