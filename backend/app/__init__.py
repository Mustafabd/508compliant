from dotenv import load_dotenv

# Load backend/.env (if present) before any submodule reads os.environ at
# import time. Local dev convenience only -- Render/Railway/etc set real
# environment variables directly, where this is a no-op.
load_dotenv()
