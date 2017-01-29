"""
aiosmtplib.esmtp
================

ESMTP extension handling.
"""
import re
import ssl
from typing import Dict, Iterable, List, Tuple, Union

from aiosmtplib.connection import SMTPConnection
from aiosmtplib.default import _default, Default
from aiosmtplib.email import parse_address, quote_address
from aiosmtplib.response import SMTPResponse
from aiosmtplib.status import SMTPStatus
from aiosmtplib.errors import (
    SMTPDataError, SMTPException, SMTPHeloError, SMTPRecipientRefused,
    SMTPResponseException, SMTPSenderRefused, SMTPServerDisconnected,
)


__all__ = ('ESMTP',)


OLDSTYLE_AUTH_REGEX = re.compile(r'auth=(?P<auth>.*)', flags=re.I)
EXTENSIONS_REGEX = re.compile(r'(?P<ext>[A-Za-z0-9][A-Za-z0-9\-]*) ?')

DefaultNumType = Union[float, int, Default]
DefaultStrType = Union[str, Default]
DefaultSSLContextType = Union[ssl.SSLContext, Default]
NumType = Union[float, int]
ExtensionsType = Tuple[Dict[str, str], List[str]]


class ESMTP(SMTPConnection):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.last_helo_response = None
        self._last_ehlo_response = None
        self.esmtp_extensions = {}
        self.supports_esmtp = False
        self.server_auth_methods = []

    @property
    def last_ehlo_response(self):
        return self._last_ehlo_response

    @last_ehlo_response.setter
    def last_ehlo_response(self, response):
        """
        When setting the last EHLO response, parse the message for supported
        extensions and auth methods.
        """
        extensions, auth_methods = parse_esmtp_extensions(response.message)
        self._last_ehlo_response = response
        self.esmtp_extensions = extensions
        self.server_auth_methods = auth_methods
        self.supports_esmtp = True

    @property
    def is_ehlo_or_helo_needed(self):
        """
        Check if we've already recieved a response to an EHLO or HELO command.
        """
        return (
            self.last_ehlo_response is None and
            self.last_helo_response is None)

    # Base SMTP commands #

    async def helo(self, hostname: str = None, **kwargs) -> SMTPResponse:
        """
        Send the SMTP HELO command.
        Hostname to send for this command defaults to the FQDN of the local
        host.

        Raises ``SMTPHeloError`` on an unexpected server response code.
        """
        if hostname is None:
            hostname = self.source_address

        response = await self.execute_command(
            b'HELO', hostname.encode('utf-8'), **kwargs)
        self.last_helo_response = response

        if response.code != SMTPStatus.completed:
            raise SMTPHeloError(response.code, response.message)

        return response

    async def help(self, **kwargs) -> str:
        """
        Send the SMTP HELP command, which responds with help text.

        Raises ``SMTPResponseException`` on an unexpected server response code.
        """
        response = await self.execute_command(b'HELP', **kwargs)
        success_codes = (
            SMTPStatus.system_status_ok, SMTPStatus.help_message,
            SMTPStatus.completed,
        )
        if response.code not in success_codes:
            raise SMTPResponseException(response.code, response.message)

        return response.message

    async def rset(self, **kwargs) -> SMTPResponse:
        """
        Send an SMTP RSET command, which resets the server's envelope
        (the envelope contains the sender, recipient, and mail data).

        Raises ``SMTPResponseException`` on an unexpected server response code.
        """
        response = await self.execute_command(b'RSET', **kwargs)
        if response.code != SMTPStatus.completed:
            raise SMTPResponseException(response.code, response.message)

        return response

    async def noop(self, **kwargs) -> SMTPResponse:
        """
        Send an SMTP NOOP command, which does nothing.

        Raises ``SMTPResponseException`` on an unexpected server response code.
        """
        response = await self.execute_command(b'NOOP', **kwargs)
        if response.code != SMTPStatus.completed:
            raise SMTPResponseException(response.code, response.message)

        return response

    async def vrfy(self, address: str, **kwargs) -> SMTPResponse:
        """
        Send an SMTP VRFY command, which tests an address for validity.
        Not many servers support this command.

        Raises ``SMTPResponseException`` on an unexpected server response code.
        """
        parsed_address = parse_address(address)

        response = await self.execute_command(
            b'VRFY', parsed_address.encode('utf-8'), **kwargs)

        success_codes = (
            SMTPStatus.completed, SMTPStatus.will_forward,
            SMTPStatus.cannot_vrfy,
        )

        if response.code not in success_codes:
            raise SMTPResponseException(response.code, response.message)

        return response

    async def expn(self, address: str, **kwargs) -> SMTPResponse:
        """
        Send an SMTP EXPN command, which expands a mailing list.
        Not many servers support this command.

        Raises ``SMTPResponseException`` on an unexpected server response code.
        """
        parsed_address = parse_address(address)

        response = await self.execute_command(
            b'EXPN', parsed_address.encode('utf-8'), **kwargs)

        if response.code != SMTPStatus.completed:
            raise SMTPResponseException(response.code, response.message)

        return response

    async def quit(self, **kwargs) -> SMTPResponse:
        """
        Send the SMTP QUIT command, which closes the connection.
        Also closes the connection from our side after a response is recieved.

        Raises ``SMTPResponseException`` on an unexpected server response code.
        """
        response = await self.execute_command(b'QUIT', **kwargs)
        if response.code != SMTPStatus.closing:
            raise SMTPResponseException(response.code, response.message)

        self.close()

        return response

    async def mail(
            self, sender: str, options: Iterable[str] = None,
            **kwargs) -> SMTPResponse:
        """
        Send an SMTP MAIL command, which specifies the message sender and
        begins a new mail transfer session ("envelope").

        Raises ``SMTPSenderRefused`` on an unexpected server response code.
        """
        if options is None:
            options = []

        options_bytes = [option.encode('utf-8') for option in options]
        from_string = b'FROM:' + quote_address(sender).encode('utf-8')

        response = await self.execute_command(
            b'MAIL', from_string, *options_bytes, **kwargs)

        if response.code != SMTPStatus.completed:
            raise SMTPSenderRefused(response.code, response.message, sender)

        return response

    async def rcpt(
            self, recipient: str, options: Iterable[str] = None,
            **kwargs) -> SMTPResponse:
        """
        Send an SMTP RCPT command, which specifies a single recipient for
        the message. This command is sent once per recipient and must be
        preceeded by 'MAIL'.

        Raises ``SMTPRecipientRefused`` on an unexpected server response code.
        """
        if options is None:
            options = []

        options_bytes = [option.encode('utf-8') for option in options]
        to = b'TO:' + quote_address(recipient).encode('utf-8')

        response = await self.execute_command(
            b'RCPT', to, *options_bytes, **kwargs)

        success_codes = (SMTPStatus.completed, SMTPStatus.will_forward)
        if response.code not in success_codes:
            raise SMTPRecipientRefused(
                response.code, response.message, recipient)

        return response

    async def data(
            self, message: Union[str, bytes],
            timeout: DefaultNumType = _default) -> SMTPResponse:
        """
        Send an SMTP DATA command, followed by the message given.
        This method transfers the actual email content to the server.

        Raises ``SMTPDataError`` on an unexpected server response code.
        """
        if timeout is _default:
            timeout = self.timeout

        if isinstance(message, str):
            message = message.encode('utf8')

        start_response = await self.execute_command(b'DATA', timeout=timeout)

        if start_response.code != SMTPStatus.start_input:
            raise SMTPDataError(start_response.code, start_response.message)

        try:
            await self.protocol.write_message_data(  # type: ignore
                message, timeout=timeout)
            response = await self.protocol.read_response(  # type: ignore
                timeout=timeout)
        except SMTPServerDisconnected as exc:
            self.close()
            raise exc

        if response.code != SMTPStatus.completed:
            raise SMTPDataError(response.code, response.message)

        return response

    # ESMTP commands #

    async def ehlo(self, hostname: str = None, **kwargs) -> SMTPResponse:
        """
        Send the SMTP EHLO command.
        Hostname to send for this command defaults to the FQDN of the local
        host.
        """
        if hostname is None:
            hostname = self.source_address

        response = await self.execute_command(
            b'EHLO', hostname.encode('utf-8'), **kwargs)
        self.last_ehlo_response = response

        if response.code != SMTPStatus.completed:
            raise SMTPHeloError(response.code, response.message)

        return response

    def supports_extension(self, extension: str) -> bool:
        """
        Tests if the server supports the ESMTP service extension given.
        """
        return extension.lower() in self.esmtp_extensions

    async def _ehlo_or_helo_if_needed(self) -> None:
        """
        Call self.ehlo() and/or self.helo() if needed.

        If there has been no previous EHLO or HELO command this session, this
        method tries ESMTP EHLO first.
        """
        if self.is_ehlo_or_helo_needed:
            try:
                await self.ehlo()
            except SMTPHeloError:
                await self.helo()

    def _reset_server_state(self) -> None:
        """
        Clear stored information about the server.
        """
        self.last_helo_response = None
        self._last_ehlo_response = None
        self.esmtp_extensions = {}
        self.supports_esmtp = False
        self.server_auth_methods = []

    async def starttls(
            self, server_hostname: str = None, validate_certs: bool = None,
            client_cert: DefaultStrType = _default,
            client_key: DefaultStrType = _default,
            tls_context: DefaultSSLContextType = _default,
            timeout: DefaultNumType = _default) -> SMTPResponse:
        """
        Puts the connection to the SMTP server into TLS mode.

        If there has been no previous EHLO or HELO command this session, this
        method tries ESMTP EHLO first.

        If the server supports TLS, this will encrypt the rest of the SMTP
        session. If you provide the keyfile and certfile parameters,
        the identity of the SMTP server and client can be checked (if
        validate_certs is True). You can also provide a custom SSLContext
        object. If no certs or SSLContext is given, and TLS config was
        provided when initializing the class, STARTTLS will use to that,
        otherwise it will use the Python defaults.
        """
        if validate_certs is not None:
            self.validate_certs = validate_certs
        if timeout is _default:
            timeout = self.timeout
        if client_cert is not _default:
            self.client_cert = client_cert  # type: ignore
        if client_key is not _default:
            self.client_key = client_key  # type: ignore
        if tls_context is not _default:
            self.tls_context = tls_context  # type: ignore

        if self.tls_context and self.client_cert:
            raise ValueError(
                'Either a TLS context or a certificate/key must be provided')

        if server_hostname is None:
            server_hostname = self.hostname

        tls_context = self._get_tls_context()

        await self._ehlo_or_helo_if_needed()

        if not self.supports_extension('starttls'):
            raise SMTPException(
                'SMTP STARTTLS extension not supported by server.')

        try:
            response, protocol = await self.protocol.starttls(  # type: ignore
                tls_context, server_hostname=server_hostname, timeout=timeout)
        except SMTPServerDisconnected as exc:
            self.close()
            raise exc
        self.transport = protocol._app_transport

        if response.code == SMTPStatus.ready:
            # RFC 3207 part 4.2:
            # The client MUST discard any knowledge obtained from
            # the server, such as the list of SMTP service extensions,
            # which was not obtained from the TLS negotiation itself.
            self._reset_server_state()

        return response


def parse_esmtp_extensions(message: str) -> ExtensionsType:
    """
    Parse an EHLO response from the server into a dict of {extension: params}
    and a list of auth method names.

    It might look something like:
         220 size.does.matter.af.MIL (More ESMTP than Crappysoft!)
         EHLO heaven.af.mil
         250-size.does.matter.af.MIL offers FIFTEEN extensions:
         250-8BITMIME
         250-PIPELINING
         250-DSN
         250-ENHANCEDSTATUSCODES
         250-EXPN
         250-HELP
         250-SAML
         250-SEND
         250-SOML
         250-TURN
         250-XADR
         250-XSTA
         250-ETRN
         250-XGEN
         250 SIZE 51200000
    """
    esmtp_extensions = {}
    auth_types = []  # type: List[str]

    response_lines = message.split('\n')

    # ignore the first line
    for line in response_lines[1:]:
        # To be able to communicate with as many SMTP servers as possible,
        # we have to take the old-style auth advertisement into account,
        # because:
        # 1) Else our SMTP feature parser gets confused.
        # 2) There are some servers that only advertise the auth methods we
        #    support using the old style.
        auth_match = OLDSTYLE_AUTH_REGEX.match(line)
        if auth_match:
            auth_type = auth_match.group('auth')[0]
            if auth_type not in auth_types:
                auth_types.append(auth_type.lower().strip())

        # RFC 1869 requires a space between ehlo keyword and parameters.
        # It's actually stricter, in that only spaces are allowed between
        # parameters, but were not going to check for that here.  Note
        # that the space isn't present if there are no parameters.
        extensions = EXTENSIONS_REGEX.match(line)
        if extensions:
            extension = extensions.group('ext').lower()
            params = extensions.string[extensions.end('ext'):].strip()
            esmtp_extensions[extension] = params

            if extension == 'auth':
                auth_types.extend(
                    [param.strip().lower() for param in params.split()])

    return esmtp_extensions, auth_types
