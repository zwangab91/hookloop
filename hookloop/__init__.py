"""hookloop — an agent that runs the daily short-form content loop end to end.

Pipeline (mirrors the 7-step "AI content creator" playbook):
  1. pick a hot topic (rotating daily)            -> steps/topic.py
  2. search the topic's hashtags                  -> steps/research.py
  3. keep only videos with 1M+ views              -> steps/research.py
  4. study their first 10 seconds (hook library)  -> steps/hooks.py
  5. write + render a <=45s video                 -> steps/script.py, steps/render.py
  6. publish daily to every surface               -> steps/publish.py
  7. track growth, flag the 10k monetization line -> steps/growth.py
"""

__version__ = "0.1.0"
