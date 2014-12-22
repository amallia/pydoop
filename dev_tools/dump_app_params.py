"""
Dump app options in rst table format.
"""

import sys
import argparse

import pydoop.app.main


def set_option_attrs(actions):
    for a in actions:
        opts = a.option_strings
        assert len(opts) > 0
        try:
            a.short_opt, a.long_opt = opts
        except ValueError:
            o = opts[0]
            assert o.startswith('-')
            if o.startswith('--'):
                a.short_opt, a.long_opt = None, o
            else:
                a.short_opt, a.long_opt = o, None


def get_col_widths(actions):
    lengths = {}
    for a in actions:
        for n in 'short_opt', 'long_opt', 'help':
            attr = getattr(a, n)
            lengths.setdefault(n, []).append(0 if attr is None else len(attr))
    widths = dict((k, max(v)) for k, v in lengths.iteritems())
    # add 4 for ``backticks``
    for n in 'short_opt', 'long_opt':
        widths[n] += 4
    return widths


class Formatter(object):

    NAMES = 'short_opt', 'long_opt', 'help'

    def __init__(self, actions):
        self.col_widths = get_col_widths(actions)
        self.actions = actions

    def format_line(self, fields):
        l = [f.ljust(self.col_widths[n]) for f, n in zip(fields, self.NAMES)]
        return '| %s |' % ' | '.join(l)

    def format_action(self, action):
        l = []
        for n in 'short_opt', 'long_opt':
            opt = getattr(action, n)
            l.append('``%s``' % opt if opt else '')
        l.append(getattr(action, 'help'))
        return self.format_line(l)

    def hline(self, filler='-'):
        l = []
        for n in self.NAMES:
            l.append(filler * self.col_widths[n])
        return '+{0}{1}{0}+'.format(
            filler, '{0}+{0}'.format(filler).join(l)
        )

    def header_lines(self):
        lines = [self.hline()]
        lines.append(self.format_line(['Short', 'Long', 'Meaning']))
        lines.append(self.hline(filler='='))
        return lines

    def dump_table(self, outf, exclude_h=True):
        for l in self.header_lines():
            outf.write(l+'\n')
        for a in self.actions:
            if exclude_h and a.short_opt == '-h':
                continue
            outf.write(self.format_action(a)+'\n')
            outf.write(self.hline()+'\n')
        outf.write(self.hline()+'\n')


def make_parser():
    parser = argparse.ArgumentParser(description='dump pydoop app help')
    parser.add_argument('-o', '--out-fn', metavar='FILE', help='output file')
    parser.add_argument('--app', metavar='PYDOOP_APP_NAME', default='script')
    return parser


def main():
    parser = make_parser()
    args = parser.parse_args()
    #--
    pydoop_parser = pydoop.app.main.make_parser()
    subp = pydoop_parser._pydoop_docs_helper[args.app]
    act_map = dict((_.title, _._group_actions) for _ in subp._action_groups)
    actions = act_map['optional arguments']
    set_option_attrs(actions)
    fmt = Formatter(actions)
    try:
        outf = open(args.out_fn) if args.out_fn else sys.stdout
        fmt.dump_table(outf)
    finally:
        if args.out_fn:
            outf.close()


if __name__ == '__main__':
    main()
