"""One explicit SMTP envelope mailbox per configured report recipient."""
from email.utils import getaddresses


def normalize_recipients(value):
    if not isinstance(value,list) or not value:
        raise ValueError('Report recipients must be a nonempty list')
    result=[]
    for entry in value:
        if not isinstance(entry,str) or any(c in entry for c in ('\r','\n')):
            raise ValueError('Report recipient must contain one mailbox')
        addresses=getaddresses([entry])
        if len(addresses)!=1:
            raise ValueError('Report recipient must contain one mailbox')
        address=addresses[0][1]
        if address.count('@')!=1 or any(c.isspace() for c in address):
            raise ValueError('Report recipient must contain one mailbox')
        local,domain=address.split('@')
        if not local or not domain:
            raise ValueError('Report recipient must contain one mailbox')
        result.append(address)
    return result
