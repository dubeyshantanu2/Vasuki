import re

with open("main.py", "r") as f:
    content = f.read()

# 1. Add sync_fallback_records at the beginning of initialize()
content = re.sub(
    r'(logger\.info\("Initializing OrderFlowSystem\.\.\."\)\n\s+try:\n)',
    r'\1        await self.supabase.sync_fallback_records(discord_client=self.discord)\n',
    content
)

# 2. Update save_ calls to be wrapped in asyncio.create_task
replacements = [
    (r'self\.supabase\.save_market_structure\((.+?)\)', r'asyncio.create_task(self.supabase.save_market_structure(\1))'),
    (r'self\.supabase\.save_volume_profile\((.+?)\)', r'asyncio.create_task(self.supabase.save_volume_profile(\1))'),
    (r'self\.supabase\.save_big_trade\((.+?)\)', r'asyncio.create_task(self.supabase.save_big_trade(\1))'),
    (r'self\.supabase\.save_signal\((.+?)\)', r'asyncio.create_task(self.supabase.save_signal(\1))'),
    (r'self\.supabase\.save_delta_candle\((.+?)\)', r'asyncio.create_task(self.supabase.save_delta_candle(\1))'),
    (r'self\.supabase\.mark_signal_invalidated\((.+?)\)', r'asyncio.create_task(self.supabase.mark_signal_invalidated(\1))'),
]

for old, new in replacements:
    content = re.sub(old, new, content)

with open("main.py", "w") as f:
    f.write(content)

