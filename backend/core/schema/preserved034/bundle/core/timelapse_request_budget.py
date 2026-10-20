"""Private request-wide verification allowance; never supplied by JSON."""
import time
from modules.archives.timelapse_retirement import _refuse
class VerificationBudget:
    def __init__(self):self.deadline=time.monotonic()+60;self.remaining=32*(1<<30)
    def check(self):
        if time.monotonic()>=self.deadline:_refuse('manifest_limit')
    def consume(self,count):
        self.check()
        if type(count) is not int or count<0 or count>self.remaining:_refuse('manifest_limit')
        self.remaining-=count
