import re

with open('core/signal_engine.py', 'r') as f:
    lines = f.readlines()

new_lines = []
in_evaluate = False
for i, line in enumerate(lines):
    if line.startswith('def evaluate('):
        in_evaluate = True
    
    if in_evaluate:
        # If the line is 'def evaluate(' or unindented parameters or unindented body up to 'results: List[GateResult] = []'
        if line.startswith('def evaluate(') or \
           line.startswith('    self,') or \
           line.startswith('    current_price') or \
           line.startswith(') -> Tuple') or \
           line.startswith('    """') or \
           line.startswith('    Runs all 4 gates') or \
           line.startswith('    if self.expiry_config') or \
           line.startswith('        return None') or \
           line.startswith('    self._purge_expired_cooldowns') or \
           line.startswith('    results: List[GateResult]'):
            new_lines.append('    ' + line)
        else:
            new_lines.append(line)
        
        if line.startswith('    results: List[GateResult] = []'):
            in_evaluate = False
    else:
        new_lines.append(line)

with open('core/signal_engine.py', 'w') as f:
    f.writelines(new_lines)
