# part 3 dashboard - 90 day market activity
# uses the csv from metrics.py and saves the dashboard as an image

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch

SRC = "data/serve/dashboard_90d.csv"

# thressholds for volume changes
AMBER_PCT = 0.15
RED_PCT= 0.30

w= pd.read_csv(SRC, parse_dates=["trade_date"])
w = w.rename(columns={"trade_date":"Date", "asx_close":"close", "vix_close":"vix"})

# converting volume as yahoo has it in thousands
w["volume"] = w["asx_volume"] * 1000
w["vol_avg_20d"]= w["vol_avg_20d"] * 1000

w["vol_vs_avg"] = w["volume"] / w["vol_avg_20d"] - 1

# assign colour status depending on how much volume increased
w["rag"] = np.select(
    [w.vol_vs_avg > RED_PCT, w.vol_vs_avg > AMBER_PCT],
    ["Red", "Amber"], default="Green"
)

print(w["rag"].value_counts())

plt.rcParams["font.family"] = "DejaVu Sans"

ACC= '#1F4E79'
GREY='#B8B8B8'
DK = '#333333'
AMB='#E8A317'
RED = '#C0392B'
GRN='#2E8B57'

pk= w.loc[w.close.idxmax()]
tr= w.loc[w.close.idxmin()]
last =w.iloc[-1]


# get rid of  chart borders
def clean(ax):
    for s in ['top','right','left']:
        ax.spines[s].set_visible(False)

    ax.spines['bottom'].set_color(GREY)
    ax.tick_params(length=0,colors=DK)
    ax.grid(False)


def build():

    fig= plt.figure(figsize=(11,9.2))

    gs= fig.add_gridspec(4,1,height_ratios=[0.75,3.1,2.2,1.5],
                          hspace=0.55,left=.07,right=.93,top=.93,bottom=.06)

    fig.text(.07,.965,
             'Market activity is normal today but heavy volume days are clustering again',
             fontsize=15,fontweight='bold',color=DK,ha='left')


    # status box at top
    c = fig.add_subplot(gs[0])
    c.axis('off')
    c.set_xlim(0,1)
    c.set_ylim(0,1)

    c.add_patch(FancyBboxPatch((0,.05),.17,.9,
                boxstyle='round,pad=0.01,rounding_size=0.03',
                fc=GRN,ec='none'))

    c.text(.085,.5,'GREEN',color='white',fontsize=17,
           fontweight='bold',ha='center',va='center')

    c.text(.19,.78,
           f"{last.Date:%d %b}: volume is {abs(last.vol_vs_avg)*100:.1f}% {'above' if last.vol_vs_avg>=0 else 'below'} its 20-day average, inside the normal band.",
           fontsize=11.5,color=DK,va='center')

    c.text(.19,.22,
           "Watch: 4 of the last 10 sessions were amber or red (16, 19, 21, 23 Oct).\nAmber = 15-30% above average; red = more than 30% above.",
           fontsize=9.5,color='#666666',va='center',linespacing=1.4)


    # volume graph
    a= fig.add_subplot(gs[1])
    clean(a)

    col= [RED if r=='Red' else AMB if r=='Amber' else GREY for r in w.rag]

    a.bar(w.Date,w.volume/1e6,color=col,width=.8)
    a.plot(w.Date,w.vol_avg_20d/1e6,color=ACC,lw=2.2)

    a.set_title('Volume has run close to its 20-day average, with four red-flag spikes',
                loc='left',fontsize=12,fontweight='bold',color=DK,pad=8)

    a.set_ylabel('Volume (millions)',color=DK)

    a.xaxis.set_major_locator(mdates.MonthLocator())
    a.xaxis.set_major_formatter(mdates.DateFormatter('%b'))
    a.tick_params(labelbottom=True)

    a.text(w.Date.iloc[-1]+pd.Timedelta(days=1),
           w.vol_avg_20d.iloc[-1]/1e6,'20-day\naverage',
           color=ACC,fontsize=9.5,va='center',fontweight='bold')

    # label two big spikes
    for dt,lab in [(pd.Timestamp('2026-09-18'),'18 Sep: +127%'),
                   (pd.Timestamp('2026-08-31'),'31 Aug: +95%')]:

        y =w.loc[w.Date==dt,'volume'].iloc[0]/1e6
        a.annotate(lab,(dt,y),xytext=(0,6),
                   textcoords='offset points',ha='center',fontsize=9,color=RED)

    a.set_xlim(w.Date.iloc[0]-pd.Timedelta(days=2),
               w.Date.iloc[-1]+pd.Timedelta(days=14))

    # ASX close prices
    b= fig.add_subplot(gs[2],sharex=a)
    clean(b)

    b.axvspan(pk.Date,tr.Date,color='#EEEEEE',zorder=0)
    b.plot(w.Date,w.close,color=DK,lw=1.8)

    b.set_title('The index fell 7% from its August peak but volatility barely moved',
               loc='left',fontsize=12,fontweight='bold',color=DK,pad=8)

    b.set_ylabel('ASX 200 close',color=DK)
    b.tick_params(labelbottom=True)

    b.annotate(f'Peak {pk.close:,.0f} (6 Aug)',(pk.Date,pk.close),
               xytext=(-8,-4),textcoords='offset points',
               ha='right',fontsize=9,color=DK)

    b.set_ylim(8450,9400)

    b.annotate(f'Low {tr.close:,.0f} (1 Oct)',(tr.Date,tr.close),
               xytext=(10,-14),textcoords='offset points',
               ha='left',fontsize=9,color=DK)

    b.text(w.Date.iloc[-1]+pd.Timedelta(days=1),last.close,
           f'{last.close:,.0f}',fontsize=9.5,va='center',
           color=DK,fontweight='bold')


    # VIX chart at bottom
    v =fig.add_subplot(gs[3],sharex=a)
    clean(v)

    v.axvspan(pk.Date,tr.Date,color='#EEEEEE',zorder=0)
    v.plot(w.Date,w.vix,color=ACC,lw=1.8)
    v.set_ylim(12,22)

    v.set_title('VIX (previous-day US close) stayed in a calm 14-21 range',
                loc='left',fontsize=12,fontweight='bold',color=DK,pad=8)

    v.set_ylabel('VIX',color=DK)
    v.tick_params(labelbottom=True)

    v.text(w.Date.iloc[-1]+pd.Timedelta(days=1),last.vix,
           f'{last.vix:.1f}',fontsize=9.5,va='center',
           color=ACC,fontweight='bold')


    # data sources
    fig.text(.07,.012,
             'Source: Yahoo Finance (S&P/ASX 200), FRED (VIXCLS). 90 trading days -25 Jun to 29 Oct 2026. Grey shading = 6 Aug peak to 1 Oct low.',
             fontsize=8.5,color='#777777')

    fig.savefig('dashboard.png',dpi=150)
    plt.close()


build()
