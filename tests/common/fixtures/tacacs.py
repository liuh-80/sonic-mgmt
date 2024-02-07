import os
import logging
import pytest
from tests.tacacs.utils  import setup_tacacs_client, setup_tacacs_server,load_tacacs_creds,\
                    cleanup_tacacs, restore_tacacs_servers, print_tacacs_creds

logger = logging.getLogger(__name__)


def drop_all_ssh_session(duthost):
    try:
        duthost.shell('sudo ss -K sport ssh')
    except:
        pass


@pytest.fixture(scope="module")
def tacacs_creds(creds_all_duts):
    hardcoded_creds = load_tacacs_creds()
    creds_all_duts.update(hardcoded_creds)
    return creds_all_duts


@pytest.fixture(scope="module", autouse=True)
def setup_tacacs(ptfhost, duthosts, enum_rand_one_per_hwsku_hostname, tacacs_creds, creds):
    print_tacacs_creds(tacacs_creds)
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    # Setup TACACS config according to flag defined in /ansible/group_vars/lab/lab.yml
    use_lab_tacacs_server = creds['test_with_lab_tacacs_server']
    tacacs_server_ip = ""
    tacacs_server_passkey = ""
    if use_lab_tacacs_server:
        tacacs_server_ip = creds['lab_tacacs_server']
        tacacs_server_passkey = creds['lab_tacacs_passkey']
    else:
        tacacs_server_ip = ptfhost.mgmt_ip
        tacacs_server_passkey = tacacs_creds[duthost.hostname]['tacacs_passkey']

    logger.debug("Setup TACACS server: use_lab_tacacs_server:{}, tacacs_server_ip:{}, tacacs_server_passkey:{}"
                 .format(use_lab_tacacs_server, tacacs_server_ip, tacacs_server_passkey))

    # setup per-command authorization with 'tacacs+ local', when command blocked by TACACS server, UT still can pass.
    setup_tacacs_client(duthost, tacacs_creds, tacacs_server_ip, tacacs_server_passkey, "\"tacacs+ local\"")
    
    if not use_lab_tacacs_server:
        setup_tacacs_server(ptfhost, tacacs_creds, duthost)

    # After TACACS enabled, only new ssh session control by TACACS.
    # Ansible use connection pool, existing connection in pool not control by TACACAS
    # Drop all connection to force ansible re-connect.
    drop_all_ssh_session(duthost)

    yield

    cleanup_tacacs(ptfhost, tacacs_creds, duthost)

    if not use_lab_tacacs_server:
        restore_tacacs_servers(duthost)
