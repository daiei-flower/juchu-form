from PIL import Image, ImageDraw, ImageFont
import numpy as np
F='fonts/'; U='/root/.claude/uploads/d255a743-fc0c-558d-a67e-334dd9b7bbec/'
def hx(h,a=255): h=h.lstrip('#'); return (int(h[:2],16),int(h[2:4],16),int(h[4:],16),a)
def key(path, hole=False):
    a=np.asarray(Image.open(path).convert('RGB')).astype(np.float32)
    r,g,b=a[...,0],a[...,1],a[...,2]; sp=g-np.maximum(r,b); al=np.clip(1-(sp-30)/90,0,1)
    rgb=np.stack([r,np.minimum(g,np.maximum(r,b)+5),b],-1)
    if hole:
        hh,ww=al.shape; yy,xx=np.mgrid[0:hh,0:ww]; m=((xx-ww/2)**2+(yy-hh/2)**2)<(0.1*ww)**2
        al[m]=1; rgb[m]=[38,40,46]
    return Image.fromarray(np.dstack([rgb,al*255]).astype(np.uint8),'RGBA')
G=dict(header=(239,255,1491,372),art=(246,443,1484,1206),text=(228,1263,1503,1710),
       cells=[(245,397),(463,615),(680,832),(898,1050),(1116,1267),(1333,1486)],cy=(1777,1929),
       segs=[426,517,607,697,787,878,967,1061,1147,1237],sw=62,sy=(2040,2157),wl=330,wr=1400,wy=2100)
frame=key(U+'fe90911e-image.jpg'); frame.save('nb_frame_training_v3.png')
W,H=frame.size; M=60
c=Image.new('RGBA',(W+2*M,H+2*M),(0,0,0,0)); c.alpha_composite(frame,(M,M)); d=ImageDraw.Draw(c)
# art
ax0,ay0,ax1,ay1=G['art']; bw,bh=ax1-ax0,ay1-ay0
art=Image.open('gen/art_bench_press.png').convert('RGBA'); A=8
big=art.resize((art.width*A,art.height*A),Image.NEAREST)
cw=min(bw,big.width); ch=min(bh,big.height)
big=big.crop(((big.width-cw)//2,(big.height-ch)//2,(big.width-cw)//2+cw,(big.height-ch)//2+ch))
c.alpha_composite(big,(M+ax0+(bw-cw)//2,M+ay0+(bh-ch)//2))
d.rectangle([M+ax0,M+ay1-70,M+ax1,M+ay1],fill=hx('1A1613',200))
fm=ImageFont.truetype(F+'MPLUS1p-Bold.ttf',44)
gem=Image.open('gen/gem_common.png').convert('RGBA'); gem=gem.crop(gem.getbbox()); gem=gem.resize((gem.width*4,gem.height*4),Image.NEAREST)
lab='トレーニング'; tw=d.textlength(lab,font=fm)+gem.width+16; cx=M+(ax0+ax1)/2; cy=M+ay1-35
c.alpha_composite(gem,(int(cx-tw/2),int(cy-gem.height/2))); d.text((cx-tw/2+gem.width+16,cy),lab,font=fm,fill=hx('FFF8EF'),anchor='lm')
# header
hx0,hy0,hx1,hy1=G['header']; hcy=M+(hy0+hy1)/2
d.text((M+hx0+210,hcy),'ベンチプレス',font=ImageFont.truetype(F+'DelaGothicOne-Regular.ttf',84),fill=hx('FFF8EF'),anchor='lm')
ic=Image.open('gen/icon_barbell.png').convert('RGBA'); ic=ic.crop(ic.getbbox()); ic=ic.resize((ic.width*5,ic.height*5),Image.NEAREST)
plate=(M+hx1-ic.width-50,M+hy0+10,M+hx1-10,M+hy1-10); d.rounded_rectangle(plate,radius=14,fill=hx('F2B233'))
c.alpha_composite(ic,(int((plate[0]+plate[2])/2-ic.width/2),int((plate[1]+plate[3])/2-ic.height/2)))
# cost plate
pl=key(U+'32f76ab7-image.jpg',hole=True); pl=pl.crop(pl.getbbox()); PS=250; pl=pl.resize((PS,PS),Image.LANCZOS)
c.alpha_composite(pl,(M-30,M-30))
d.text((M-30+PS/2,M-30+PS/2+8),'2',font=ImageFont.truetype(F+'DelaGothicOne-Regular.ttf',150),fill=hx('FFF8EF'),anchor='mm',stroke_width=8,stroke_fill=hx('1A1613'))
# effect
tx0,ty0,tx1,ty1=G['text']; fb=ImageFont.truetype(F+'MPLUS1p-ExtraBold.ttf',96)
for i,ln in enumerate(['8ダメージ','相手に動揺2']):
    d.text((M+(tx0+tx1)/2,M+(ty0+ty1)/2+(i-0.5)*120),ln,font=fb,fill=hx('1E1A17'),anchor='mm')
# cells
fk=ImageFont.truetype(F+'MPLUS1p-ExtraBold.ttf',64); cy0,cy1=G['cy']
parts=[('胸','main',2),('背','off',0),('肩','sub',1),('腕','sub',1),('腹','off',0),('脚','off',0)]
for (x0,x1),(ja,st,n) in zip(G['cells'],parts):
    if st!='off': d.rounded_rectangle([M+x0+4,M+cy0+4,M+x1-4,M+cy1-4],radius=8,fill=hx('C9492F') if st=='main' else hx('F1C9B8'))
    tc=hx('FFF8EF') if st=='main' else hx('5E1D0F') if st=='sub' else hx('B5ABA3'); mx=M+(x0+x1)/2
    d.text((mx,M+cy0+62),ja,font=fk,fill=tc,anchor='mm')
    for k in range(n):
        dx=mx+(k-(n-1)/2)*26; d.ellipse([dx-8,M+cy1-40,dx+8,M+cy1-24],fill=tc)
# gauge
on=round(80/100*len(G['segs'])); sy0,sy1=G['sy']
for x0 in G['segs'][:on]: d.rectangle([M+x0+4,M+sy0+4,M+x0+G['sw']-4,M+sy1-4],fill=hx('E8603F'))
d.text((M+G['wl'],M+G['wy']),'重量',font=ImageFont.truetype(F+'MPLUS1p-ExtraBold.ttf',56),fill=hx('D8CABB'),anchor='mm')
d.text((M+G['wr'],M+G['wy']-24),'80',font=ImageFont.truetype(F+'DelaGothicOne-Regular.ttf',84),fill=hx('FFF8EF'),anchor='mm')
d.text((M+G['wr'],M+G['wy']+46),'/ 100kg',font=ImageFont.truetype(F+'MPLUS1p-Bold.ttf',40),fill=hx('D8CABB'),anchor='mm')
c.save('nb_card_training_full_v3.png')
bg=Image.new('RGBA',c.size,hx('E9E6DF')); bg.alpha_composite(c)
bg.convert('RGB').resize((c.width//2,c.height//2),Image.LANCZOS).save('nb_card_training_preview_v3.png'); print('ok')
