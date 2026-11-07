"""Names for trusted SQL fragments that cannot shadow their outer references."""
import re


def inner_alias(name, expression):
    identifiers = set(re.findall(r'[A-Za-z_][A-Za-z_0-9]*', expression.lower()))
    while name.lower() in identifiers:
        name += '_inner'
    return name
