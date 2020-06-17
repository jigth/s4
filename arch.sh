#!/bin/bash
set -xeuo pipefail

if (! which gcc || ! which pypy3 || ! which nc || ! which git) &>/dev/null; then
    sudo pacman --noconfirm --noprogressbar -Sy \
         gcc \
         git \
         man \
         pypy3 \
         python
fi

if ! sudo python3 -m pip; then
    sudo python3 -m ensurepip
fi

if ! sudo pypy3 -m pip; then
    sudo pypy3 -m ensurepip
fi

cd /mnt

(
    if [ ! -d s4 ]; then
        git clone https://github.com/nathants/s4
    fi
    cd s4
    if [ ! -f /tmp/requirements.done ]; then
        sudo python3 -m pip install -r requirements.txt
        sudo pypy3   -m pip install -r requirements.txt
        touch /tmp/requirements.done
    fi
    sudo python3 setup.py develop
    sudo pypy3   setup.py develop
)

if ! which xxh3 &>/dev/null; then
    git clone https://github.com/nathants/bsv
    (
        cd bsv
        make
        sudo mv -fv bin/* /usr/local/bin
    )
fi
