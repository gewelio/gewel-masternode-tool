FROM fedora
MAINTAINER \
[Gewel <noreply@gewel.io>]
RUN yum update -y \
 && yum group install -y "Development Tools" "Development Libraries" \
 && yum install -y redhat-rpm-config python3-devel libusbx-devel libudev-devel cmake gcc-c++ \
 && yum remove -y gmp-devel \
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
 && set +H \
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
