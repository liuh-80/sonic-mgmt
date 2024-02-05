import logging
import pytest
from ansible.errors import AnsibleConnectionFailure
from tests.tacacs.utils import setup_tacacs_client, setup_tacacs_server, load_tacacs_creds,\
                    cleanup_tacacs, restore_tacacs_servers, print_tacacs_creds


logger = logging.getLogger(__name__)
TACACS_CREDS_FILE = 'tacacs_creds.yaml'


def drop_all_ssh_session(duthost):
    try:
        duthost.shell('sudo ss -K sport ssh')
    except AnsibleConnectionFailure:
        # Current connection will throw AnsibleConnectionFailure after connection broken
        pass


@pytest.fixture(scope="module")
def tacacs_creds(creds_all_duts):
    hardcoded_creds = load_tacacs_creds()
    creds_all_duts.update(hardcoded_creds)
    return creds_all_duts


@pytest.fixture(scope="module", autouse=True)
def setup_tacacs(ptfhost, duthosts, enum_rand_one_per_hwsku_hostname, tacacs_creds):
    print_tacacs_creds(tacacs_creds)
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    tacacs_server_ip = ptfhost.mgmt_ip
    setup_tacacs_client(duthost, tacacs_creds, tacacs_server_ip)
    setup_tacacs_server(ptfhost, tacacs_creds, duthost)

    # After TACACS enabled, only new ssh session control by TACACS.
    # Ansible use connection pool, existing connection in pool not control by TACACAS
    # Drop all connection to force ansible re-connect.
    drop_all_ssh_session(duthost)

    yield

    cleanup_tacacs(ptfhost, tacacs_creds, duthost)
    restore_tacacs_servers(duthost)