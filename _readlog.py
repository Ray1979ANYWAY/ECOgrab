# -*- coding: utf-8 -*-
import io, re
log = r'D:\Documents\eazyVid\eazyvid.log'
out = r'D:\Documents\eazyVid\_log_tail3.txt'
lines = io.open(log, encoding='utf-8', errors='replace').read().splitlines()
ctrl = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
with io.open(out, 'w', encoding='utf-8', newline='\n') as f:
    f.write('TOTAL %d\n' % len(lines))
    for l in lines[-50:]:
        l = ctrl.sub('?', l)
        if len(l) > 240:
            l = l[:240] + '...'
        f.write(l + '\n')
print('written', len(lines))
