import os
import sys
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

URL = os.environ["STREAMLIT_APP_URL"]   # app link, passed in by the workflow
WAKE_XPATH = "//button[contains(., 'get this app back up')]"

MAX_ATTEMPTS = 8        # how many times to try before failing
WAIT_BETWEEN = 60       # seconds to rest between attempts

def make_driver():
    # run Chrome without a screen (needed on GitHub's servers)
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)

def visit_once():
    driver = make_driver()
    try:
        driver.get(URL)
        time.sleep(20)                       # let the page load

        # print what the robot actually sees, so the log is real evidence
        page_text = driver.find_element(By.TAG_NAME, "body").text
        print("Page title:", driver.title)
        print("Page text starts with:", page_text[:150].replace("\n", " "))

        buttons = driver.find_elements(By.XPATH, WAKE_XPATH)
        sleeping = "gone to sleep" in page_text.lower()

        if buttons or sleeping:
            if not buttons:
                print("Page says the app is asleep but no button was found.")
                return False                 # fail loudly so you hear about it

            print("App was asleep. Clicking the wake-up button.")
            buttons[0].click()
            # wait up to 3 minutes for the app to boot (button disappears)
            for _ in range(36):
                time.sleep(5)
                if not driver.find_elements(By.XPATH, WAKE_XPATH):
                    break
            else:
                return False                 # still asleep, this try failed
        else:
            print("App already awake.")

        time.sleep(45)                       # stay on the page so it counts as a visit
        return True
    except Exception as error:               # a crash counts as a failed try
        print(f"Error during attempt: {error}")
        return False
    finally:
        driver.quit()

for attempt in range(1, MAX_ATTEMPTS + 1):
    print(f"Attempt {attempt} of {MAX_ATTEMPTS}")
    if visit_once():
        print("Done. App is awake.")
        sys.exit(0)
    time.sleep(WAIT_BETWEEN)

print("FAILED: could not wake the app after all attempts.")
sys.exit(1)   # a failed run makes GitHub email you