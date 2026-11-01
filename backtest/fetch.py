import httpx, json, asyncio, os
B="https://stats.fn.sportradar.com/sportybet/en/Africa:Lagos/gismo"
H={"Referer":"https://www.sportybet.com/","Origin":"https://www.sportybet.com","User-Agent":"Mozilla/5.0"}
S=json.load(open('seasons.json'))
FD={0:"2627",1:"2526",2:"2425"}
async def main():
  sem=asyncio.Semaphore(6)
  async with httpx.AsyncClient(headers=H,timeout=60,follow_redirects=True) as c:
    async def gis(ut,sid,i):
      fn=f"gismo_{ut}_{sid}.json"
      if os.path.exists(fn): return
      async with sem:
        d=(await c.get(f"{B}/stats_season_fixtures2/{sid}/1")).json()['doc'][0]['data']
      ms=[]
      for m in d.get('matches') or []:
        r=m.get('result') or {}; p=m.get('periods') or {}
        if m.get('canceled') or m.get('postponed') or r.get('home') is None: continue
        ft=p.get('ft') or r; p1=p.get('p1') or {}
        ms.append({"id":m['_id'],"uts":m['time']['uts'],"home":m['teams']['home']['name'],"away":m['teams']['away']['name'],
          "huid":m['teams']['home']['uid'],"auid":m['teams']['away']['uid'],"fh":ft.get('home'),"fa":ft.get('away'),
          "hh":p1.get('home'),"ha":p1.get('away'),"period":r.get('period')})
      json.dump(ms,open(fn,'w')); print(fn,len(ms))
    async def fd(code,yr):
      fn=f"fd_{code}_{yr}.csv"
      if os.path.exists(fn): return
      async with sem:
        r=await c.get(f"https://www.football-data.co.uk/mmz4281/{yr}/{code}.csv")
      open(fn,'wb').write(r.content); print(fn,r.status_code,len(r.content))
    tasks=[]
    for ut,v in S.items():
      for i,(sid,name) in enumerate(v['seasons']):
        tasks.append(gis(ut,sid,i)); tasks.append(fd(v['code'],FD[i]))
    await asyncio.gather(*tasks)
asyncio.run(main())
