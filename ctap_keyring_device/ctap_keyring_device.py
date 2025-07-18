# -*- coding: utf-8 -*-
import base64
import time
from hashlib import sha256
from threading import Event
from types import FunctionType
from typing import List, Optional, Mapping, Any

import keyring
from cryptography.hazmat.primitives import serialization
from fido2 import cose, cbor
from fido2 import ctap
from fido2 import ctap2
from fido2 import hid
from fido2 import webauthn
from fido2.attestation import PackedAttestation
from fido2.ctap import CtapError
from fido2.ctap2 import AssertionResponse, AttestationResponse, Info
from fido2.webauthn import (
    Aaguid,
    AttestedCredentialData,
    AuthenticatorData,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialType,
    PublicKeyCredentialUserEntity,
)
from keyring.backends.fail import Keyring as FailKeyring

from ctap_keyring_device.ctap_credential_maker import CtapCredentialMaker
from ctap_keyring_device.ctap_private_key_wrapper import CtapPrivateKeyWrapper
from ctap_keyring_device.ctap_strucs import (
    CtapOptions,
    CtapGetNextAssertionContext,
    CtapMakeCredentialRequest,
    Credential,
    CtapGetAssertionRequest,
)
from ctap_keyring_device.user_verifiers.ctap_user_verifier_factory import (
    CtapUserVerifierFactory,
)


class CtapKeyringDevice(ctap.CtapDevice):
    """
    This is a virtual CTAP, FIDO2 only authenticator device, which the default keyring of your OS
    to save and retrieve the generated credentials.

    User presence and verification is supported in OSX and Windows where Touch-ID or Windows Hello are configured,
    meaning, a user will get prompted - if the user verification option is set, in these situations.

    The credentials are stored encoded and encrypted in the keychain (irregardless of the keychain's encryption),
    and without user identifying information, rendering key-steals likely to be useless.

    Decrypting the credentials requires the credential id, which is not stored on-device, but rather
    needs to be sent and stored on the RP (Relying Party) end - and be passed-in for all get-assertion
    operations as an allowed credential.

    The CTAP commands supported are:
    - MAKE_CREDENTIAL (0x01)
    - GET_ASSERTION (0x02)
    - GET_INFO (0x04)
    - GET_NEXT_ASSERTION (0x08)

    Not supported commands are:
    - CLIENT_PIN (0x06)
    - RESET (0x07)
    - CREDENTIAL_MGMT (0x41)
    """

    SUPPORTED_CTAP_VERSIONS = ['FIDO_2_0']
    MAX_MSG_SIZE = 1 << 20
    AAGUID = Aaguid(b'pasten-ctap-1337')

    def __init__(self):
        self._ctap2_cmd_to_handler = {
            ctap2.Ctap2.CMD.MAKE_CREDENTIAL: self.make_credential,
            ctap2.Ctap2.CMD.GET_ASSERTION: self.get_assertion,
            ctap2.Ctap2.CMD.GET_NEXT_ASSERTION: self.get_next_assertion,
            ctap2.Ctap2.CMD.GET_INFO: self.get_info,
        }

        algorithms = []
        for i in cose.CoseKey.supported_algorithms():
            algorithms.append({'type': 'public-key', 'alg': i})

        self._info = Info(
            versions=self.SUPPORTED_CTAP_VERSIONS,
            extensions=[],
            aaguid=self.AAGUID,
            options={
                CtapOptions.PLATFORM_DEVICE: True,
                CtapOptions.RESIDENT_KEY: True,
                CtapOptions.USER_PRESENCE: True,
                CtapOptions.USER_VERIFICATION: True,
                CtapOptions.CLIENT_PIN: True,
            },
            pin_uv_protocols=[ctap2.PinProtocolV2.VERSION],
            max_msg_size=self.MAX_MSG_SIZE,
            transports=[webauthn.AuthenticatorTransport.INTERNAL],
            algorithms=algorithms,
        )

        self._next_assertions_ctx: Optional[CtapGetNextAssertionContext] = None
        self._user_verifier = CtapUserVerifierFactory.create()

    @property
    def capabilities(self) -> int:
        return hid.CAPABILITY.CBOR

    @classmethod
    def list_devices(cls):
        if isinstance(keyring.get_keyring(), FailKeyring):
            return []

        return [cls()]

    def call(
        self,
        cmd: int,
        data: bytes = b"",
        event: Event = None,
        on_keepalive: FunctionType = None,
    ):
        # noinspection PyBroadException
        try:
            res = self._call(cmd, data, event, on_keepalive)
            return self._wrap_err_code(CtapError.ERR.SUCCESS) + cbor.encode(res)
        except CtapError as e:
            return self._wrap_err_code(e.code)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._wrap_err_code(CtapError.ERR.OTHER)

    # noinspection PyUnusedLocal
    def _call(
        self,
        cmd: int,
        data: bytes = b"",
        event: Event = None,
        on_keepalive: FunctionType = None,
    ):
        if cmd != hid.CTAPHID.CBOR:
            raise CtapError(CtapError.ERR.INVALID_COMMAND)

        if not data:
            raise CtapError(CtapError.ERR.INVALID_PARAMETER)

        ctap2_cmd = ctap2.Ctap2.CMD.from_bytes(data[:1], 'big')
        handler = self._ctap2_cmd_to_handler.get(ctap2_cmd)
        if not handler:
            raise CtapError(CtapError.ERR.INVALID_COMMAND)

        if len(data) == 1:
            return handler()

        # noinspection PyBroadException
        try:
            ctap2_req: Mapping[Any, Any] = cbor.decode(data[1:])
        except Exception:
            import traceback

            traceback.print_exc()
            raise CtapError(CtapError.ERR.INVALID_CBOR)

        if not isinstance(ctap2_req, dict):
            import traceback

            traceback.print_exc()
            raise CtapError(CtapError.ERR.INVALID_CBOR)

        # noinspection PyArgumentList
        return handler(ctap2_req)

    @staticmethod
    def _wrap_err_code(err: CtapError.ERR) -> bytes:
        return err.to_bytes(1, 'big')

    def get_info(self) -> Info:
        return self._info

    def make_credential(self, make_credential_request: dict) -> AttestationResponse:
        # noinspection PyBroadException
        request = CtapMakeCredentialRequest.create(make_credential_request)
        if (
            not request.rp
            or not request.rp.id
            or not request.user
            or not request.user.id
            or not request.client_data_hash
        ):
            raise CtapError(CtapError.ERR.MISSING_PARAMETER)

        if request.exclude_list:
            found_creds = self._find_credentials(request.exclude_list, request.rp.id)
            if found_creds:
                raise CtapError(CtapError.ERR.CREDENTIAL_EXCLUDED)

        cred = self._create_credential(request)
        attested_data = self._make_attested_credential_data(cred)
        authenticator_data = self._make_authenticator_data(
            request.rp.id, attested_data
        )
        signature = self._generate_signature(
            authenticator_data, request.client_data_hash, cred.private_key
        )

        attestation_statement = {'alg': cred.algorithm, 'sig': signature}
        attestation_response = AttestationResponse(
            PackedAttestation.FORMAT, authenticator_data, attestation_statement
        )
        return attestation_response

    @classmethod
    def _create_credential(cls, request: CtapMakeCredentialRequest) -> Credential:
        if not request.public_key_credential_params:
            raise CtapError(CtapError.ERR.MISSING_PARAMETER)

        cred_param, cose_key_cls = None, cose.UnsupportedKey
        for cred_param in request.public_key_credential_params:
            cose_key_cls = cose.CoseKey.for_alg(cred_param.alg)
            if cose_key_cls != cose.UnsupportedKey:
                break

        if cred_param is None or cose_key_cls == cose.UnsupportedKey:
            raise CtapError(CtapError.ERR.UNSUPPORTED_ALGORITHM)

        try:
            cred_maker = CtapCredentialMaker(cose_key_cls)
            cred = cred_maker.make_credential(request.user.id)

            # Do note that the combination of some keyrings with some COSE keys could fail, due to the password
            # exceeding the max cred size; for instance, WinCred supports passwords up to 512-bytes, while
            # an encoded 2048-bits RSA key would exceed that.
            # The least error-prone, safest overall key to use is a 256-bit (non-Koblitz) elliptic curve (ES256)
            service_name = cls.get_service_name(request.rp.id)
            import hashlib
            user_uuid = cred.id[:16]
            safe_username = hashlib.sha256(user_uuid).hexdigest()[:32]
            keyring.set_password(
                service_name=service_name, username=safe_username, password=cred.encoded
            )
            return cred
        except Exception:
            import traceback

            traceback.print_exc()
            raise CtapError(CtapError.ERR.OTHER)

    def _make_attested_credential_data(
        self, credential: Credential
    ) -> AttestedCredentialData:
        return AttestedCredentialData.create(
            self.AAGUID, credential.id, credential.cose_key
        )

    def _make_authenticator_data(
        self, rp_id: str, attested_credential_data: Optional[AttestedCredentialData]
    ) -> AuthenticatorData:
        flags = (
            AuthenticatorData.FLAG.USER_PRESENT | AuthenticatorData.FLAG.USER_VERIFIED
        )
        if attested_credential_data:
            flags |= AuthenticatorData.FLAG.ATTESTED

        rp_id_hash = sha256(rp_id.encode('utf-8')).digest()
        sig_counter = self._get_timestamp_signature_counter()
        return AuthenticatorData.create(
            rp_id_hash, flags, sig_counter, attested_credential_data or b''
        )

    @staticmethod
    def _get_timestamp_signature_counter() -> int:
        return int(time.time())

    @staticmethod
    def _generate_signature(
        authenticator_data: AuthenticatorData,
        client_data_hash: bytes,
        signer: CtapPrivateKeyWrapper,
    ):
        return signer.sign(data=authenticator_data + client_data_hash)

    def get_assertion(self, get_assertion_request: dict) -> AssertionResponse:
        request = CtapGetAssertionRequest.create(get_assertion_request)
        
        if request.user_verification_required:
            self._verify_user(request.rp_id)

        creds = self._find_credentials(request.allow_list, request.rp_id)

        if len(creds) == 0:
            raise CtapError(CtapError.ERR.NO_CREDENTIALS)

        # Note: consecutive calls to get_assertion will override this context
        self._next_assertions_ctx = CtapGetNextAssertionContext(
            request=request, creds=creds, cred_counter=0
        )

        return self._get_assertion(request, self._next_assertions_ctx)

    @classmethod
    def _find_credentials(
        cls, allow_list: List[PublicKeyCredentialDescriptor], rp_id: str
    ) -> List[Credential]:
        service_name = cls.get_service_name(rp_id)
        if not allow_list:
            # Currently, we only support get assertion flows where a credential id is supplied;
            # because we ought to pass in a user-id to the backend keyring; this user-id is encoded in the cred-id.
            raise CtapError(CtapError.ERR.MISSING_PARAMETER)

        res = []
        for i, allowed_cred in enumerate(allow_list):
            valid_cred = allowed_cred.id and len(allowed_cred.id) == 32
            if not valid_cred:
                continue

            user_uuid, key_password = allowed_cred.id[:16], allowed_cred.id[16:]
            # noinspection PyBroadException
            try:
                import hashlib
                safe_username = hashlib.sha256(user_uuid).hexdigest()[:32]
                
                # Try new hashed format first
                encoded_password = keyring.get_password(
                    service_name=service_name, username=safe_username
                )
                
                # Fall back to old format for backward compatibility
                if not encoded_password:
                    encoded_password = keyring.get_password(
                        service_name=service_name, username=user_uuid.hex()
                    )
                
                if not encoded_password:
                    continue

                decoded_password = base64.b64decode(encoded_password)

                alg = int.from_bytes(decoded_password[:2], 'big', signed=True)
                cose_key_cls = cose.CoseKey.for_alg(alg)
                if cose_key_cls == cose.UnsupportedKey:
                    continue

                private_key_bytes = decoded_password[2:]
                private_key = serialization.load_der_private_key(
                    private_key_bytes, password=key_password
                )

                signer = CtapPrivateKeyWrapper.create(cose_key_cls, private_key)
                cred = Credential(allowed_cred.id, signer)
                res.append(cred)
            except Exception:
                # Best effort
                import traceback

                traceback.print_exc()
                continue

        return res

    @classmethod
    def get_service_name(cls, rp_id: str) -> str:
        return '{rp_id}-webauthn'.format(rp_id=rp_id)

    def get_next_assertion(self) -> AssertionResponse:
        if not self._next_assertions_ctx:
            raise CtapError(CtapError.ERR.NOT_ALLOWED)

        return self._get_assertion(
            self._next_assertions_ctx.request, self._next_assertions_ctx
        )

    def _get_assertion(
        self, request: CtapGetAssertionRequest, ctx: CtapGetNextAssertionContext
    ) -> AssertionResponse:
        try:
            cred = ctx.get_next_cred()
            authenticator_data = self._make_authenticator_data(
                request.rp_id, attested_credential_data=None
            )
            signature = self._generate_signature(
                authenticator_data, request.client_data_hash, cred.private_key
            )

            credential_descriptor = PublicKeyCredentialDescriptor(
                type=PublicKeyCredentialType.PUBLIC_KEY,
                id=cred.id,
                transports=[webauthn.AuthenticatorTransport.INTERNAL],
            )
            user_entity = PublicKeyCredentialUserEntity(
                name='', id=cred.id[:16], display_name=''
            )
            credential_dict = {
                "type": credential_descriptor.type,
                "id": credential_descriptor.id,
                "transports": credential_descriptor.transports
            }
            user_dict = {
                "id": user_entity.id,
                "name": user_entity.name,
                "displayName": user_entity.display_name
            }
            response = AssertionResponse(
                credential_dict,
                authenticator_data,
                signature,
                user=user_dict,
                number_of_credentials=len(ctx.creds),
            )
            return response
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise

    def _verify_user(self, rp_id: str):
        verified = self._user_verifier.verify_user(rp_id)
        if not verified:
            raise CtapError(CtapError.ERR.NOT_ALLOWED)
