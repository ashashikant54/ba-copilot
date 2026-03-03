#!/bin/bash
cd /home/site/wwwroot
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 2