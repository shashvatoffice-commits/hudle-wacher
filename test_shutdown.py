import unittest
from unittest.mock import patch
import watch

class ShutdownTests(unittest.TestCase):
    def test_sender_never_reads_credentials_or_contacts_network(self):
        with patch.object(watch, '_secret', side_effect=AssertionError('credential access')), patch.object(watch.urllib.request, 'urlopen', side_effect=AssertionError('network access')) as network:
            self.assertFalse(watch.telegram_send({}, 'offline verification'))
            network.assert_not_called()

    def test_entrypoint_exits_before_configuration_network_or_state(self):
        with patch.object(watch.Path, 'read_text', side_effect=AssertionError('file read')), patch.object(watch.Path, 'write_text', side_effect=AssertionError('file write')), patch.object(watch.urllib.request, 'urlopen', side_effect=AssertionError('network access')), patch.object(watch, 'telegram_send', side_effect=AssertionError('send attempted')):
            watch.main()

if __name__ == '__main__':
    unittest.main()
