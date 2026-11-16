import os
import sys
import time
import pandas as pd
from selenium import webdriver
from bs4 import BeautifulSoup


def _import_paths():
    cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        folder = os.path.join(cur, "scripts")
        if os.path.isfile(os.path.join(folder, "project_paths.py")):
            if folder not in sys.path:
                sys.path.insert(0, folder)
            import project_paths
            return project_paths
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    sys.exit("Could not find scripts/project_paths.py")


paths = _import_paths()


def main():
    # 2. Define the range and the shorter base URL
    doc_ids = range(2077, 2112)
    VOLUME = 1  # set the volume number here for record-keeping
    out_csv = os.path.join(paths.RAW_DIR, f"raw_vol{int(VOLUME):02d}.csv")
    if os.path.exists(out_csv) and "--overwrite" not in sys.argv:
        sys.exit(
            f"{out_csv} already exists. "
            "Set VOLUME to a volume that has not been scraped, or pass --overwrite to replace it. "
            f"doc_ids is currently a {len(doc_ids)}-id range, not a full volume."
        )

    # 1. Setup the browser
    driver = webdriver.Chrome()
    base_url = "https://go-gale-com.proxy.library.nd.edu/ps/retrieve.do?resultListType=RELATED_DOCUMENT&userGroupName=nd_ref&inPS=true&contentSegment=&prodId=GVRL&isETOC=true&currentPosition=&docId=GALE|CX28309"
    data = []

    # 3. Open the first page to initiate login
    driver.get(f"{base_url}{doc_ids[0]}")
    print("Please log in to the library portal in the browser window, then return here and press Enter.")
    input()

    # 4. Loop through the IDs sequentially
    for i in doc_ids:
        # The :05d tells Python to make the number exactly 5 digits long, adding leading zeros if needed.
        url = f"{base_url}{i:05d}"
        driver.get(url)
        time.sleep(3)  # Safe delay to let the network load

        # 5. Extract content using BeautifulSoup
        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # --- EXTRACT TITLE ---
        title_element = soup.find('span', class_='citation-title')
        name = title_element.get_text(strip=True) if title_element else "Unknown Entry"

        # --- SEQUENTIAL EXTRACTION LOGIC ---
        content_paras = []
        source_paras = []
        is_sources_section = False

        # Find all h2 tags AND div tags in the document order
        for element in soup.find_all(['h2', 'div']):
            # Check if we hit the pivot header
            if element.name == 'h2' and 'BIBLIOGRAPHY' in element.get_text(strip=True).upper():
                is_sources_section = True
                continue  # Skip to the next element

            # Check if the element is one of our target paragraphs
            if element.name == 'div' and element.get('class') and 'SideNotePara' in element.get('class'):
                para_text = element.get_text(strip=True)

                # Sort into the correct bucket based on the switch
                if is_sources_section:
                    source_paras.append(para_text)
                else:
                    content_paras.append(para_text)

        # Merge arrays into unified text blocks
        full_content = "\n\n".join(content_paras)
        full_sources = "\n\n".join(source_paras)

        # Store the results if the page actually loaded a title
        if title_element:
            data.append({
                'Name': name,
                'Content': full_content,
                'Sources': full_sources
            })
            print(f"Scraped ID {i}: {name} | Content: {len(content_paras)} paras | Sources: {len(source_paras)} paras")
        else:
            print(f"Skipped ID {i}: Elements not found. Check if the login session expired.")

    # 6. Save to CSV
    df = pd.DataFrame(data)
    df.to_csv(out_csv, index=False)
    print(f"\nSuccess! Data compiled and exported to '{out_csv}'.")
    driver.quit()


if __name__ == "__main__":
    main()
