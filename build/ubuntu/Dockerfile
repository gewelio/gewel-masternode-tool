FROM ubuntu:17.10
MAINTAINER \
[Gewel <noreply@gewel.io>]
RUN apt-get update \
 && apt-get -y upgrade \
 && apt-get -y install libudev-dev libusb-1.0-0-dev libfox-1.6-dev autotools-dev autoconf automake libtool libpython3-all-dev python3.6-dev python3-pip git cmake \
 && pip3 install virtualenv \
 && cd ~ \
 && mkdir gmt \
 && cd gmt \
 && virtualenv -p python3 venv \
 && . venv/bin/activate \
 && pip install --upgrade setuptools \
 && git clone https://github.com/gewelio/gewel-masternode-tool \
 && cd gewel-masternode-tool/ \
 && pip install -r requirements.txt \
 && cd ~/gmt \
 && echo "#!/bin/sh" | tee build-gmt.sh \
 && echo "cd ~/gmt" | tee -a build-gmt.sh \
 && echo ". venv/bin/activate" | tee -a build-gmt.sh \
 && echo "cd gewel-masternode-tool" | tee -a build-gmt.sh \
 && echo "git fetch --all" | tee -a build-gmt.sh \
 && echo "git reset --hard origin/master" | tee -a build-gmt.sh \
 && echo "pip install -r requirements.txt" | tee -a build-gmt.sh \
 && echo "pyinstaller --distpath=../dist/linux --workpath=../dist/linux/build gewel_masternode_tool.spec" | tee -a build-gmt.sh \
 && echo "cd .." | tee -a build-gmt.sh \
 && chmod +x build-gmt.sh

CMD ~/gmt/build-gmt.sh
