import json
import random

from base58 import b58encode_check

from aeternity.exceptions import NameNotAvailable, PreclaimFailed, TooEarlyClaim, ClaimFailed, AENSException, \
    UpdateError, MissingPreclaim, InsufficientFundsException, AException
from aeternity.oracle import Oracle
from aeternity.epoch import EpochClient
from aeternity.signing import SignableTransaction


class NameStatus:
    REVOKED = 'REVOKED'
    UNKNOWN = 'UNKNOWN'
    AVAILABLE = 'AVAILABLE'
    PRECLAIMED = 'PRECLAIMED'
    CLAIMED = 'CLAIMED'
    TRANSFERRED = 'TRANSFERRED'


class AEName:
    Status = NameStatus

    def __init__(self, domain, client=None):
        if client is None:
            client = EpochClient()
        self.__class__.validate_name(domain)

        self.client = client
        self.domain = domain
        self.status = NameStatus.UNKNOWN
        # set after preclaimed:
        self.preclaimed_block_height = None
        self.preclaimed_commitment_hash = None
        self.preclaim_salt = None
        # set after claimed
        self.name_ttl = 0
        self.pointers = []

    @property
    def name_hash(self):
        return self._encode_name(self.domain)

    @classmethod
    def validate_pointer(cls, pointer):
        return (
            not cls.validate_address(pointer, raise_exception=False)
            or
            not cls.validate_name(pointer, raise_exception=False)
        )

    @classmethod
    def validate_address(cls, address, raise_exception=True):
        if not address.startswith(('ak$', 'ok$')):
            if raise_exception:
                raise ValueError(
                    'pointer addresses must start with in ak$'
                )
            return False
        return True

    @classmethod
    def validate_name(cls, domain, raise_exception=True):
        # TODO: validate according to the spec!
        # TODO: https://github.com/aeternity/protocol/blob/master/AENS.md#name
        if not domain.endswith(('.aet', '.test')):
            if raise_exception:
                raise ValueError('AENS TLDs must end in .aet')
            return False
        return True

    def update_status(self):
        try:
            response = self.client.external_http_get('name', params={'name': self.domain})
            self.status = NameStatus.CLAIMED
            self.name_ttl = response['name_ttl']
            self.pointers = json.loads(response['pointers'])
        except NameNotAvailable:
            # e.g. if the status is already PRECLAIMED or CLAIMED, don't reset
            # it to AVAILABLE.
            if self.status == NameStatus.UNKNOWN:
                self.status = NameStatus.AVAILABLE

    def is_available(self):
        self.update_status()
        return self.status == NameStatus.AVAILABLE

    def check_claimed(self):
        self.update_status()
        return self.status == NameStatus.CLAIMED

    def full_claim_blocking(self, keypair, preclaim_fee=1, claim_fee=1):
        if not self.is_available():
            raise NameNotAvailable(self.domain)
        self.preclaim(keypair, fee=preclaim_fee)
        self.claim_blocking(keypair, fee=claim_fee)

    def _get_commitment_hash(self, salt):
        response = self.client.external_http_get(
            'commitment-hash',
            params={
                'name': self.domain,
                'salt': salt
            }
        )
        try:
            return response['commitment']
        except KeyError:
            raise PreclaimFailed(response)

    def preclaim(self, keypair, fee=1, salt=None,):
        # check which block we used to create the preclaim
        self.preclaimed_block_height = self.client.get_height()
        if salt is not None:
            self.preclaim_salt = salt
        else:
            self.preclaim_salt = random.randint(0, 2**64)

        commitment_hash = self._get_commitment_hash(self.preclaim_salt)

        preclaim_transaction = self.client.external_http_post(
            '/tx/name/preclaim',
            json=dict(
                commitment=commitment_hash,
                fee=fee,
                account=keypair.get_address(),
            )
        )
        signable_preclaim_tx = SignableTransaction(preclaim_transaction)
        signed_transaction, b58signature = keypair.sign_transaction(signable_preclaim_tx)
        self.client.send_signed_transaction(signed_transaction)
        self.status = AEName.Status.PRECLAIMED
        return preclaim_transaction['tx_hash'], self.preclaim_salt

    def claim_blocking(self, keypair, fee=1):
        try:
            self.claim(keypair, fee=fee)
        except TooEarlyClaim:
            self.client.wait_for_next_block()
            self.claim(keypair, fee=fee)

    @classmethod
    def _encode_name(cls, name):
        return 'nm$' + b58encode_check(name.encode('ascii'))

    def claim(self, keypair, fee=1):
        if self.preclaimed_block_height is None:
            raise MissingPreclaim('You must call preclaim before claiming a name')

        current_block_height = self.client.get_height()
        if self.preclaimed_block_height >= current_block_height:
            raise TooEarlyClaim(
                'You must wait for one block to call claim.'
                'Use `claim_blocking` if you have a lot of time on your hands'
            )

        claim_transaction = self.client.external_http_post(
            '/tx/name/claim',
            json=dict(
                account=keypair.get_address(),
                name=AEName._encode_name(self.domain),
                name_salt=self.preclaim_salt,
                fee=fee,
            )
        )
        signable_claim_tx = SignableTransaction(claim_transaction)
        signed_transaction, b58signature = keypair.sign_transaction(signable_claim_tx)
        self.client.send_signed_transaction(signed_transaction)
        self.status = AEName.Status.CLAIMED
        return claim_transaction['tx_hash'], self.preclaim_salt

    def update(self, target, ttl=50, fee=1):
        assert self.status == NameStatus.CLAIMED, 'Must be claimed to update pointer'

        if isinstance(target, Oracle):
            if target.oracle_id is None:
                raise ValueError('You must register the oracle before using it as target')
            target = target.oracle_id

        if target.startswith('ak'):
            pointers = {'account_pubkey': target}
        else:
            pointers = {'oracle_pubkey': target}

        # self.client.external_http_post(
        #     'tx/name/update',
        #     json=dict(
        #         fee=fee,
        #         name_hash=self.name_hash,
        #         name_ttl=ttl,
        #         pointers=pointers,
        #         ttl=ttl,
        #     )
        # )

        response = self.client.internal_http_post(
            'name-update-tx',
            json={
                "name_hash": self.name_hash,
                "name_ttl": self.name_ttl,
                "ttl": ttl,
                "pointers": json.dumps(pointers),
                "fee": fee
            }
        )
        if 'name_hash' in response:
            self.pointers = pointers
        else:
            raise UpdateError(response)

    def transfer_ownership(self, receipient_pubkey, fee=1):
        response = self.client.internal_http_post(
            'name-transfer-tx',
            json={
                "name_hash": self.name_hash,
                "recipient_pubkey": receipient_pubkey,
                "fee": fee
            }
        )
        if 'name_hash' not in response:
            raise AENSException('transfer ownership failed', payload=response)
        self.status = NameStatus.TRANSFERRED

    def revoke(self, fee=1):
        response = self.client.internal_http_post(
            'name-revoke-tx',
            json={
                "name_hash": self.name_hash,
                "fee": fee
            }
        )
        if 'name_hash' in response:
            self.status = NameStatus.REVOKED
        else:
            raise AENSException('Error revoking name', payload=response)
