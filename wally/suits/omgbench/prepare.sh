#!/bin/bash

set -e
set -x

#if [ -d /tmp/venv ]; then
#exit 0
#fi

rm -rf /tmp/venv
rm -rf /tmp/oslo.messaging

apt-get update
apt-get -y install git python-pip virtualenv python-dev

cd /tmp
mkdir venv
cd venv
virtualenv --no-setuptools .

cd /tmp
#git clone http://github.com/openstack/oslo.messaging
git clone https://github.com/ozamiatin/oslo.messaging.git

source venv/bin/activate
pip install setuptools
pip install eventlet PyYAML oslo.messaging petname redis zmq pika_pool
cd oslo.messaging
git fetch https://github.com/ozamiatin/oslo.messaging.git gateway && git checkout FETCH_HEAD
pip install .
