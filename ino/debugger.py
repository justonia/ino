import sys

try:
    import ipdb
    def set_trace():
        ipdb.set_trace()
    def pm(t):
        ipdb.post_mortem(t)

except ImportError,e:
    import pdb
    def set_trace():
        pdb.Pdb(skip=['ino.ipdb']).set_trace()

    def pm(t):
        pdb.post_mortem(t)

