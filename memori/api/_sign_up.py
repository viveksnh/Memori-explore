r"""
 __  __                           _
|  \/  | ___ _ __ ___   ___  _ __(_)
| |\/| |/ _ \ '_ ` _ \ / _ \| '__| |
| |  | |  __/ | | | | | (_) | |  | |
|_|  |_|\___|_| |_| |_|\___/|_|  |_|
                  perfectam memoriam
                       memorilabs.ai
"""

import sys

from requests.exceptions import HTTPError

from memori._config import Config
from memori._network import Api


class Manager:
    def __init__(self, config: Config):
        self.config = config

    def execute(self, email: str | None = None):
        if email is None and len(sys.argv) > 2:
            email = sys.argv[2]

        if not email:
            self.usage()
            return

        try:
            response = Api(self.config).post("sdk/account", {"email": email})
            print(response.get("content", "You're all set! We sent you an email."))
        except HTTPError as e:
            if e.response.status_code != 422:
                raise

            print(f'The email you provided "{email}" is not valid.')

        print("")

    def usage(self):
        print("memori sign-up <email_address>")
