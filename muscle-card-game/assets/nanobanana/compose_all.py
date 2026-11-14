from PIL import Image, ImageDraw, ImageFont
import numpy as np
F='fonts/'; U='/root/.claude/uploads/d255a743-fc0c-558d-a67e-334dd9b7bbec/'
def hx(h,a=255): h=h.lstrip('#'); return (int(h[:2],16),int(h[2:4],16),int(h[4:],16),a)
def font(n,s): return ImageFont.truetype(F+n,s)
def key(path, hole=False):
    a=np.asarray(Image.open(path).convert('RGB')).astype(np.float32)
    r,g,b=a[...,0],a[...,1],a[...,2]; sp=g-np.maximum(r,b); al=np.clip(1-(sp-30)/90,0,1)
    rgb=np.stack([r,np.minimum(g,np.maximum(r,b)+5),b],-1)
    if hole:
        hh,ww=al.shape; yy,xx=np.mgrid[0:hh,0:ww]; m=((xx-ww/2)**2+(yy-hh/2)**2)<(0.1*ww)**2
        al[m]=1; rgb[m]=[38,40,46]
    return Image.fromarray(np.dstack([rgb,al*255]).astype(np.uint8),'RGBA')
FR={
 'training':dict(src='fe90911e-image.jpg',plate=(45,50),header=(239,255,1491,372),art=(246,443,1484,1206),text=(228,1263,1503,1710),
   cells=[(245,397),(463,615),(680,832),(898,1050),(1116,1267),(1333,1486)],cy=(1777,1929),
   segs=[426,517,607,697,787,878,967,1061,1147,1237],sw=62,sy=(2040,2157),wl=330,wr=1400,wy=2100),
 'method':dict(src='4a42d0ba-image.jpg',plate=(40,40),header=(237,237,1498,347),art=(268,406,1464,1158),text=(246,1240,1485,2188)),
 'mental':dict(src='e405beac-image.jpg',plate=(28,30),header=(204,223,1529,328),art=(229,398,1504,1153),text=(228,1238,1524,2209)),
}
PLATE=None
def card(kind,name,cost,art,eq,gem,label,effect,parts=None,weight=None):
    global PLATE
    g=FR[kind]; frame=key(U+g['src']); frame.save(f'nb_frame_{kind}.png')
    W,H=frame.size; M=60
    c=Image.new('RGBA',(W+2*M,H+2*M),(0,0,0,0)); c.alpha_composite(frame,(M,M)); d=ImageDraw.Draw(c)
    ax0,ay0,ax1,ay1=g['art']; bw,bh=ax1-ax0,ay1-ay0
    a=Image.open('gen/'+art).convert('RGBA'); A=8; big=a.resize((a.width*A,a.height*A),Image.NEAREST)
    cw,ch=min(bw,big.width),min(bh,big.height)
    big=big.crop(((big.width-cw)//2,(big.height-ch)//2,(big.width-cw)//2+cw,(big.height-ch)//2+ch))
    c.alpha_composite(big,(M+ax0+(bw-cw)//2,M+ay0+(bh-ch)//2))
    d.rectangle([M+ax0,M+ay1-70,M+ax1,M+ay1],fill=hx('1A1613',200))
    fm=font('MPLUS1p-Bold.ttf',44)
    gm=Image.open('gen/'+gem).convert('RGBA'); gm=gm.crop(gm.getbbox()); k=max(1,48//gm.height); gm=gm.resize((gm.width*k,gm.height*k),Image.NEAREST)
    tw=d.textlength(label,font=fm)+gm.width+16; cx=M+(ax0+ax1)/2; cy=M+ay1-35
    c.alpha_composite(gm,(int(cx-tw/2),int(cy-gm.height/2))); d.text((cx-tw/2+gm.width+16,cy),label,font=fm,fill=hx('FFF8EF'),anchor='lm')
    hx0,hy0,hx1,hy1=g['header']; hcy=M+(hy0+hy1)/2
    fsz=84 if len(name)<=6 else 72
    d.text((M+hx0+210,hcy),name,font=font('DelaGothicOne-Regular.ttf',fsz),fill=hx('FFF8EF'),anchor='lm')
    if eq:
        ic=Image.open('gen/'+eq).convert('RGBA'); ic=ic.crop(ic.getbbox()); ic=ic.resize((ic.width*5,ic.height*5),Image.NEAREST)
        pr=(M+hx1-ic.width-50,M+hy0+10,M+hx1-10,M+hy1-10); d.rounded_rectangle(pr,radius=14,fill=hx('F2B233'))
        c.alpha_composite(ic,(int((pr[0]+pr[2])/2-ic.width/2),int((pr[1]+pr[3])/2-ic.height/2)))
    if PLATE is None:
        p=key(U+'32f76ab7-image.jpg',hole=True); PLATE=p.crop(p.getbbox()).resize((250,250),Image.LANCZOS)
    px0,py0=M+g.get('plate',(0,0))[0],M+g.get('plate',(0,0))[1]
    c.alpha_composite(PLATE,(px0,py0))
    fc=font('DelaGothicOne-Regular.ttf',150); t=str(cost)
    l,tp,r,b=d.textbbox((0,0),t,font=fc,stroke_width=8)
    d.text((px0+125-(l+r)/2,py0+125-(tp+b)/2),t,font=fc,fill=hx('FFF8EF'),stroke_width=8,stroke_fill=hx('1A1613'))
    tx0,ty0,tx1,ty1=g['text']; fb=font('MPLUS1p-ExtraBold.ttf',96)
    for i,ln in enumerate(effect):
        d.text((M+(tx0+tx1)/2,M+(ty0+ty1)/2+(i-(len(effect)-1)/2)*120),ln,font=fb,fill=hx('1E1A17'),anchor='mm')
    if parts:
        fk=font('MPLUS1p-ExtraBold.ttf',64); cy0,cy1=g['cy']
        for (x0,x1),(ja,st,n) in zip(g['cells'],parts):
            if st!='off': d.rounded_rectangle([M+x0+4,M+cy0+4,M+x1-4,M+cy1-4],radius=8,fill=hx('C9492F') if st=='main' else hx('F1C9B8'))
            tc=hx('FFF8EF') if st=='main' else hx('5E1D0F') if st=='sub' else hx('B5ABA3'); mx=M+(x0+x1)/2
            d.text((mx,M+cy0+62),ja,font=fk,fill=tc,anchor='mm')
            for kk in range(n):
                dx=mx+(kk-(n-1)/2)*26; d.ellipse([dx-8,M+cy1-40,dx+8,M+cy1-24],fill=tc)
    if weight:
        cur,goal=weight; on=round(cur/goal*len(g['segs'])); sy0,sy1=g['sy']
        for x0 in g['segs'][:on]: d.rectangle([M+x0+4,M+sy0+4,M+x0+g['sw']-4,M+sy1-4],fill=hx('E8603F'))
        d.text((M+g['wl'],M+g['wy']),'重量',font=font('MPLUS1p-ExtraBold.ttf',56),fill=hx('D8CABB'),anchor='mm')
        d.text((M+g['wr'],M+g['wy']-24),str(cur),font=font('DelaGothicOne-Regular.ttf',84),fill=hx('FFF8EF'),anchor='mm')
        d.text((M+g['wr'],M+g['wy']+46),f'/ {goal}kg',font=font('MPLUS1p-Bold.ttf',40),fill=hx('D8CABB'),anchor='mm')
    return c
cards=[card('training','ベンチプレス',2,'art_bench_press.png','icon_barbell.png','gem_common.png','トレーニング',['8ダメージ','相手に動揺2'],
            parts=[('胸','main',2),('背','off',0),('肩','sub',1),('腕','sub',1),('腹','off',0),('脚','off',0)],weight=(80,100)),
       card('mental','深呼吸',1,'art_deep_breath.png',None,'gem_common.png','メンタル',['フォーカス5']),
       card('method','ホエイプロテイン',1,'art_whey_protein.png',None,'gem_uncommon.png','メソッド',['毎ターン、','フォーカス+3'])]
for n,cimg in zip(['training','mental','method'],cards): cimg.save(f'nb_card_{n}.png')
cw,ch=cards[0].size; pad=80
out=Image.new('RGBA',(pad*4+cw*3,pad*2+ch),hx('E9E6DF'))
for i,cimg in enumerate(cards): out.alpha_composite(cimg,(pad+i*(cw+pad),pad))
out.convert('RGB').resize((out.width//3,out.height//3),Image.LANCZOS).save('nb_cards_all_preview.png'); print(out.size)
