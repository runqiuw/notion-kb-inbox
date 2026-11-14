"""Only activated by the isolated regression runner, never by production."""
import os

if os.environ.get('KB_OFFLINE_TEST_MODE') == '1':
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def offline_credentials(*args, **kwargs):
        raise DefaultCredentialsError('offline regression: external credentials disabled')

    google.auth.default = offline_credentials
