"""
Palo Alto Networks network_ints.py

Script triggered by a Cloudwatch event that will monitor the health of firewalls
via the "show chassis status" op command on the Trust interface.
The purpose is to assess the health of the firewall and modify an AWS route table to redirect
traffic if the firewall is down.  When I firewall goes down routes within the route table bound to the
TGW attachment will show next hop as blackhole.  The routes need to be updated to a functional eni.

This software is provided without support, warranty, or guarantee.
Use at your own risk.
"""

import logging
import ssl
import urllib
import urllib.error
import urllib.request
import xml
import time
import json
import os
import boto3
import netaddr
import xml.etree.ElementTree as et

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda')
ec2_client = boto3.client('ec2')
gcontext = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)

subnets = []


def updateDefaultRouteNextHop(hostname, api_key, subnetGateway, virtualRouter="default"):
    '''
    Updates the firewall route table with the next hop of the default gateway in the AWS subnet

    :param hostname: IP address of the firewall
    :param api_key:
    :param subnetGateway: AWS subnet gateway (First IP in the subnet range)
    :param virtualRouter: VR where we wish to apply this route
    :return: Result of API request
    '''
    xpath = "/config/devices/entry[@name='localhost.localdomain']/network/virtual-router/entry[@name='default']/routing-table/ip/static-route/entry[@name='vnets']"
    element = "<destination>10.0.0.0/8</destination><interface>ethernet1/2</interface><nexthop><ip-address>{0}</ip-address></nexthop>".format(
        subnetGateway)

    return panSetConfig(hostname, api_key, xpath, element)


def panEditConfig(hostname, api_key, xpath, element):
    '''

    :param hostname: IP address of the firewall
    :param api_key:
    :param xpath: xpath of the configuration we wish to modify
    :param element: element that we wish to modify
    :return:
    '''
    data = {
        'type': 'config',
        'action': 'edit',
        'key': api_key,
        'xpath': xpath,
        'element': element
    }
    response = makeApiCall(hostname, data)
    # process response and return success or failure?
    # Debug should print output as well?
    return response


def makeApiCall(hostname, data):
    '''
    Make api request data json object utf-8 enconded
    :param hostname:
    :param data:
    :return:
    '''
    # Todo:
    # Context to separate function?
    # check response for status codes and return reponse.read() if success
    #   Else throw exception and catch it in calling function
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = "https://" + hostname + "/api"
    encoded_data = urllib.parse.urlencode(data).encode('utf-8')
    return urllib.request.urlopen(url, data=encoded_data, context=ctx).read()


def panSetConfig(hostname, api_key, xpath, element):
    '''Function to make API call to "set" a specific configuration
    '''
    data = {
        'type': 'config',
        'action': 'set',
        'key': api_key,
        'xpath': xpath,
        'element': element
    }
    response = makeApiCall(hostname, data)
    # process response and return success or failure?
    # Debug should print output as well?
    return response


def editIpObject(hostname, api_key, name, value):
    '''Function to edit/update an existing IP Address object on a PA Node
    '''
    xpath = "/config/devices/entry[@name='localhost.localdomain']/vsys/entry[@name='vsys1']/address/entry[@name='{0}']/ip-netmask".format(
        name)
    element = "<ip-netmask>{0}</ip-netmask>".format(value)
    return panEditConfig(hostname, api_key, xpath, element)


def getApiKey(hostname, username, password):
    '''Generate API keys using username/password
    API Call: http(s)://hostname/api/?type=keygen&user=username&password=password
    '''
    data = {
        'type': 'keygen',
        'user': username,
        'password': password
    }
    response = makeApiCall(hostname, data)
    if response == 'error':
        logger.info("Got error making api call to get api key!")
        return response
    else:
        return xml.etree.ElementTree.XML(response)[0][0].text


def panCommit(hostname, api_key, message=""):
    '''Function to commit configuration changes
    '''
    data = {
        "type": "commit",
        "key": api_key,
        "cmd": "<commit>{0}</commit>".format(message)
    }
    return makeApiCall(hostname, data)


def checkPaGroupReady(username, password, paGroup):
    '''Function to check whether a PaGroup (both Nodes N1 and N2) is ready to accept API calls
    This is done by trying to generate "API" key using username/password
    '''

    fw1ip = paGroup['N1Mgmt']
    fw2ip = paGroup['N2Mgmt']
    try:
        api_key_N1 = getApiKey(fw1ip, username, password)
        api_key_N2 = getApiKey(fw2ip, username, password)
        if api_key_N1 == api_key_N2:
            return True
        else:
            print("Error: API key of both nodes of the group doesn't match ")
            return False
    except:
        print("Error while retriving API Key")
        return False


def get_gw_ip(cidr):
    ip = netaddr.IPNetwork(cidr)
    iplist = list(ip)
    return iplist[1]


def check_fw_up(gwMgmtIp, api_key):

    global gcontext

    cmd = urllib.request.Request("https://" + gwMgmtIp + "/api/?type=op&cmd=<show><chassis-ready></chassis-ready></show>&key=" + api_key)
    # Send command to fw and see if it times out or we get a response
    logger.info('[INFO]: Sending command: %s', cmd)
    try:
        response = urllib.request.urlopen(cmd, data=None, context=gcontext, timeout=5).read()
        #Now we do stuff to the gw
    except urllib.error.URLError:
        logger.info("[INFO]: No response from FW. So maybe not up!")
        return 'no'
        #sleep and check again?
    else:
        logger.info("[INFO]: FW is up!!")

    logger.info("[RESPONSE]: {}".format(response))
    resp_header = et.fromstring(response)

    if resp_header.tag != 'response':
        logger.info("[ERROR]: didn't get a valid response from firewall...maybe a timeout")
        return 'cmd_error'

    if resp_header.attrib['status'] == 'error':
        logger.info("[ERROR]: Got an error for the command")
        return 'cmd_error'

    if resp_header.attrib['status'] == 'success':
        # The fw responded with a successful command execution. So is it ready?
        for element in resp_header:
            if element.text.rstrip() == 'yes':
                # Call config gw command?
                logger.info("[INFO]: FW is ready for configure")
                return 'yes'
            else:
                return 'almost'
            # The fw is still not ready to accept commands
            # so invoke lambda again and do this all over? Or just retry command?


def terminate(success):
    global asg_name
    global asg_hookname
    global instanceId


    #log that we're terminating and why
    if (success == 'false'):
      logger.error("[ERROR]: Lambda function reporting failure to AutoScaling with error\n");
      result = "ABANDON"
    else:
      logger.info("[INFO]: Lambda function reporting success to AutoScaling.");
      result = "CONTINUE";

    logger.info("[INFO]: asg_name: {}, asg_hookname: {}, instanceId: {}".format(asg_name, asg_hookname, instanceId))
    #call autoscaling

    return


def lambda_handler(event, context):
    # this_func_name = os.environ('config-fw-func-name')
    # fw1ip = os.environ('fw1ip')
    # fw2ip = os.environ('fw2ip')
    # cidr = os.environ('vpc_cidr')

    this_func_name = 'config-fw-func-name'
    fw1ip = '127.0.0.1:14443'
    fw2ip = '127.0.0.1:14444'
    username = 'panadmin'
    password = 'Pal0Alt0123!'
    fw_untrust_int = 'Fw-Untrust-Int'
    fw1_untrust_int_ip = '192.168.11.203'
    fw2_untrust_int_ip = '192.168.12.199'
    fw1_trust_cidr = '192.168.21.0/24'
    fw2_trust_cidr = '192.168.22.0/24'
    trustAZ1_subnet_cidr = '192.168.21.0/24'
    trustAZ2_subnet_cidr = '92.168.22.0/24'
    vpc_cidr = '10.0.0.0/8'
    api_key = 'LUFRPT1qS2xCRmZ6WVMrREtrK00yUGt4dVRna2lkY1U9cmgyaE93L3VoZ2U3WUgxeFpGVE1wOUNtdlM2S0Z5Z25ObG8wbmZoNXpuWT0='



    logger.info("Got Event {}".format(event))
    config = {'Username': 'admin',
              'Password': 'password'
              }

    err = 'no'
    while (True):
        err = check_fw_up(fw1ip, api_key)
        if err == 'cmd_error':
            logger.info("[ERROR]: Command error from fw")
            terminate('false')
            return
        elif err == 'no':
            # logger.info("[INFO] FW is not up...yet")
            time.sleep(60)
            continue
        elif err == 'almost':
            # this means autocommit is happening
            time.sleep(10)
            continue
        elif err == 'yes':
            logger.info("[INFO]: FW is up")
            break




    trustAZ1_subnet_gw = get_gw_ip(trustAZ1_subnet_cidr)
    trustAZ2_subnet_gw = get_gw_ip(trustAZ2_subnet_cidr)


    updateDefaultRouteNextHop(fw1ip, api_key, trustAZ1_subnet_gw, virtualRouter="default")

    updateDefaultRouteNextHop(fw2ip, api_key, trustAZ2_subnet_gw, virtualRouter="default")

    editIpObject(fw1ip, api_key, fw_untrust_int, fw1_untrust_int_ip)
    editIpObject(fw2ip, api_key, fw_untrust_int, fw2_untrust_int_ip)


if __name__ == '__main__':
    event = {}
    context = {}
    lambda_handler(event, context)