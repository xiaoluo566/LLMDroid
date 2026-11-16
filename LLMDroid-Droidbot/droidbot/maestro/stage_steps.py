"""Internal, uniquely named filter stages; initial model plans cannot emit these."""
import re


def execution_step(step):
    if not isinstance(step, str) or not step.startswith('stage/'):
        return step
    matched = re.fullmatch(r'stage/(choose|apply|reopen)/(click:[^\s=/]+)', step)
    if matched is None:
        raise ValueError('Invalid internal filter stage step')
    return matched.group(2)
