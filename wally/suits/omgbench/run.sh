#!/bin/bash
set -m

while [[ $# > 1 ]]
do
key="$1"

case $key in
    test)
    TEST="$2"
    shift
    ;;
    num_messages)
    NUM_MESSAGES="$2"
    shift
    ;;
    clients)
    CLIENTS="$2"
    shift
    ;;
    servers)
    SERVERS="$2"
    shift
    ;;
    num_topics)
    NUM_TOPICS="$2"
    shift
    ;;
    controllers)
    CONTROLLERS="$2"
    shift
    ;;
    debug)
    DEBUG="$2"
    shift
    ;;
    pattern)
    PATTERN="$2"
    shift
    ;;
    *)
    echo "Unknown option $key"
    exit 1
    ;;
esac
shift
done

# discover url. take rabbit creads from nova.conf
rabbit_username=`cat /etc/nova/nova.conf | grep rabbit_user | grep -v "#" | awk '{print $3}'`
rabbit_password=`cat /etc/nova/nova.conf | grep rabbit_pass | grep -v "#" | awk '{print $3}'`
rabbit_hosts=`cat /etc/nova/nova.conf | grep rabbit_hosts | grep -v "#" | sed 's/rabbit_hosts//g;s/,//g;s/=//g;'`
rabbit_port=`cat /etc/nova/nova.conf | grep rabbit_port | grep -v "#" | awk '{print $3}'`
contr_hosts=`echo "$rabbit_hosts" | sed 's/:5673//g'`

hostname=`hostname -s`

if [ "$TEST" = "pika" ]
 then
  echo "[oslo_messaging_pika]" > /tmp/pika.conf
  echo "pool_max_size = 100" >> /tmp/pika.conf
  CONF_FILE_OPT="--config-file /tmp/pika.conf"
   url="pika://"
   for host in $rabbit_hosts;
    do
     url="$url$rabbit_username:$rabbit_password@$host,"
    done
fi

if [ "$TEST" = "rabbit" ]
 then
   url="rabbit://"
   for host in $rabbit_hosts;
    do
     url="$url$rabbit_username:$rabbit_password@$host,"
    done
fi

if [ "$TEST" = "zmq" ] || [ "$TEST" = "zmq-proxy" ]
 then
   url="zmq://"
   sentinel=""
   for host in $contr_hosts;
    do
     sentinel="$sentinel$host:26379, "
    done
    sentinel="{$sentinel::-2}"
fi

if [ "$TEST" = "qpid" ]
 then
   url="amqp://"
   for host in $contr_hosts;
    do
     url="$url$host:5672,"
    done
fi

mkdir -p "/tmp/testlogs/$TEST"
SERVER_LOG_FILE="/tmp/testlogs/$TEST/server-"
CLIENT_LOG_FILE="/tmp/testlogs/$TEST/client"

cd /tmp/oslo.messaging/tools
source /tmp/venv/bin/activate

# retrieve host ip
host_ip=`ip a | grep 192.168 | awk '{print $2}' | sed 's/\//\ /g' | awk '{print $1}' |head -1`

if [ "$TEST" = "zmq" ]
then
    echo "[oslo_messaging_zmq]" > /tmp/zmq.conf
    echo "rpc_zmq_host = $host_ip" >> /tmp/zmq.conf
    echo "use_router_proxy = false" >> /tmp/zmq.conf
    echo "use_pub_sub = false" >> /tmp/zmq.conf
    echo "[matchmaker_redis]" >> /tmp/zmq.conf
#    echo "sentinel_hosts = $sentinel" >> /tmp/zmq.conf
    echo "host = node-16" >> /tmp/zmq.conf
    echo "port = 6379" >> /tmp/zmq.conf
    CONF_FILE_OPT="--config-file /tmp/zmq.conf"

elif [ "$TEST" = "zmq-proxy" ]
then
    echo "[oslo_messaging_zmq]" > /tmp/zmq.conf
    echo "rpc_zmq_host = $host_ip" >> /tmp/zmq.conf
    echo "use_router_proxy = true" >> /tmp/zmq.conf
    echo "use_pub_sub = false" >> /tmp/zmq.conf
    echo "rpc_use_acks = false" >> /tmp/zmq.conf
    echo "[matchmaker_redis]" >> /tmp/zmq.conf
    echo "host = node-16" >> /tmp/zmq.conf
    echo "port = 6379" >> /tmp/zmq.conf
    CONF_FILE_OPT="--config-file /tmp/zmq.conf"
fi


PATTERN_KEY=""
if [ "$PATTERN" = "cast" ]
then
    PATTERN_KEY="--is-cast True"
elif [ "$PATTERN" = "fanout" ]
then
    PATTERN_KEY="--is-cast True --is-fanout True"
fi

DEBUG_OPT=""
if [ "$DEBUG" = "True" ]
then
    DEBUG_OPT="--debug True"
fi

# generate topics
seq_topics=`seq "$NUM_TOPICS"`
topics=`for i in $seq_topics; do echo "becnhmark_topic_$i"; done`
topics_arr=($topics)

# exclude controllers string
for h in $CONTROLLERS;
do
exclude="$exclude grep -v $h|"
done

cmd="cat /etc/hosts | grep node- | $exclude grep -v messaging| awk '{print \$3}'"
servers=`eval $cmd`
servers_arr=($servers)
IFS=,
targets=`eval echo {"${topics_arr[*]}"}.{"${servers_arr[*]}"}`
unset IFS

############### Uncomment Network loss #################
#tc qdisc add dev br-mgmt root netem loss 20%


# start servers
for i in `seq "$SERVERS"`;
 do
 python simulator.py $DEBUG_OPT $CONF_FILE_OPT  --url "$url" -tp "${topics_arr[$((i % NUM_TOPICS))]}" -s $hostname rpc-server &> "$SERVER_LOG_FILE$i" &
 done

# wait for all server processes to start
while [ "$(ps aux | grep simulator.py | grep -v grep | wc -l)" -ne  "$SERVERS" ]
do
sleep 1
done

sleep 5 # sleep for all servers to get ready

# start client
# add "--is-cast" True for cast "--is-cast --is-fanout True" for fanout to the end of command
python simulator.py $DEBUG_OPT $CONF_FILE_OPT -tg $targets --url "$url" rpc-client $PATTERN_KEY --timeout 60 -p "$CLIENTS" -m "$NUM_MESSAGES" &> "$CLIENT_LOG_FILE" &

sleep 2 # sleep for client ot start

############### Uncomment distractive #################
# sleep 5 # sleep before start distractive
# ssh node-16 'iptables -I INPUT -p tcp --dport 5673 -j REJECT'

# wait for client to finish
while [ "$(ps aux | grep simulator.py| grep rpc-client | grep -v grep)" ]
do
sleep 1
done

# wait until server processes all messages
for i in `seq "$SERVERS"`;
 do
 while [ "$(tac "$SERVER_LOG_FILE$i" | grep -m1 count | awk '{print $10}')" != "0" ]
  do
  sleep 1
  done
 done

# kill all servers process
for p in `ps aux | grep simulator.py | grep server | grep -v grep | awk '{print $2}'`;
do
kill -2 "$p"
done

#wait for servers to finish
while [ "$(ps aux | grep simulator.py| grep rpc-server | grep -v grep)" ]
do
sleep 1
done

############### Uncomment distractive #################
#ssh node-16 'iptables -D INPUT 1'

############### Uncomment Network loss #################
#tc qdisc del dev br-mgmt root

# log client file last line with stat
echo "==== wally ===="
tail -1 "$CLIENT_LOG_FILE"

# log servers file last line with stat
for i in `seq "$SERVERS"`;
do
echo "==== wally ===="
tail -1  "$SERVER_LOG_FILE$i"
done
