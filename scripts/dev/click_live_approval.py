import asyncio
import json

from overnight_stable_baseline_browser import Browser, find_page


async def main() -> None:
    browser = Browser(find_page())
    await browser.connect()
    try:
        result = await browser.evaluate(
            """(()=>{
              const groups=[...document.querySelectorAll('[class*="approvalActions"]')];
              const group=groups.at(-1);
              const buttons=group?[...group.querySelectorAll('button')]:[];
              const button=buttons.at(-1);
              if(!button||button.disabled)return {clicked:false,count:buttons.length};
              const label=button.innerText;
              button.click();
              return {clicked:true,label,count:buttons.length};
            })()"""
        )
        print(json.dumps(result, ensure_ascii=True))
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
