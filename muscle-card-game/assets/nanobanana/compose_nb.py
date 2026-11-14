from PIL import Image, ImageDraw, ImageFont
import numpy as np
SRC='/root/.claude/uploads/d255a743-fc0c-558d-a67e-334dd9b7bbec/414261cd-image.jpg'
F='fonts/'
def hx(h,a=255): h=h.lstrip('#'); return (int(h[:2],16),int(h[2:4],16),int(h[4:],16),a)
im=np.asarray(Image.open(SRC).convert('RGB')).astype(np.float32)
r,g,b=im[...,0],im[...,1],im[...,2]
spill=g-np.maximum(r,b)
alpha=np.clip(1-(spill-30)/90,0,1)
g2=np.where(spill>0,np.maximum(r,b)+np.minimum(spill,0),g)
rgb=np.stack([r,np.minimum(g,np.maximum(r,b)+5),b],-1)
frame=Image.fromarray(np.dstack([rgb,alpha*255]).astype(np.uint8),'RGBA')
frame.save('nb_frame_training.png')
W,H=frame.size; M=60
c=Image.new('RGBA',(W+2*M,H+2*M),(0,0,0,0))
# art under? art window is opaque dark; draw art on top inside window
c.alpha_composite(frame,(M,M))
art=Image.open('gen/art_bench_press.png').convert('RGBA')
A=8; big=art.resize((art.width*A,art.height*A),Image.NEAREST)
ax0,ay0,ax1,ay1=258,439,1474,1177
bw,bh=ax1-ax0,ay1-ay0
big=big.crop(((big.width-bw)//2,(big.height-bh)//2,(big.width-bw)//2+bw,(big.height-bh)//2+bh))
c.alpha_composite(big,(M+ax0,M+ay0))
d=ImageDraw.Draw(c)
# type band at bottom of art
d.rectangle([M+ax0,M+ay1-70,M+ax1,M+ay1],fill=hx('1A1613',200))
fn=ImageFont.truetype(F+'DelaGothicOne-Regular.ttf',84)
fb=ImageFont.truetype(F+'MPLUS1p-ExtraBold.ttf',96)
fm=ImageFont.truetype(F+'MPLUS1p-Bold.ttf',44)
fk=ImageFont.truetype(F+'MPLUS1p-ExtraBold.ttf',64)
gem=Image.open('gen/gem_common.png').convert('RGBA'); gem=gem.crop(gem.getbbox()); gem=gem.resize((gem.width*4,gem.height*4),Image.NEAREST)
lab='トレーニング'; tw=d.textlength(lab,font=fm)+gem.width+16; cx=M+(ax0+ax1)/2; cy=M+ay1-35
c.alpha_composite(gem,(int(cx-tw/2),int(cy-gem.height/2)))
d.text((cx-tw/2+gem.width+16,cy),lab,font=fm,fill=hx('FFF8EF'),anchor='lm')
# header: name + equipment icon
d.text((M+247+150,M+310),'ベンチプレス',font=fn,fill=hx('FFF8EF'),anchor='lm')
ic=Image.open('gen/icon_barbell.png').convert('RGBA'); ic=ic.crop(ic.getbbox()); ic=ic.resize((ic.width*5,ic.height*5),Image.NEAREST)
plate=(M+1483-ic.width-40,M+262,M+1473,M+357)
d.rounded_rectangle(plate,radius=14,fill=hx('F2B233'))
c.alpha_composite(ic,(int((plate[0]+plate[2])/2-ic.width/2),int((plate[1]+plate[3])/2-ic.height/2)))
# cost orb
orb=Image.open('gen/cost_orb.png').convert('RGBA'); orb=orb.crop(orb.getbbox()); orb=orb.resize((orb.width*7,orb.height*7),Image.NEAREST)
c.alpha_composite(orb,(M+20,M+20))
d.text((M+20+orb.width/2,M+20+orb.height/2+6),'2',font=ImageFont.truetype(F+'DelaGothicOne-Regular.ttf',120),fill=hx('2B2622'),anchor='mm')
# effect
for i,ln in enumerate(['8ダメージ','相手に動揺2']):
    d.text((M+(237+1494)/2,M+(1246+1687)/2+(i-0.5)*120),ln,font=fb,fill=hx('1E1A17'),anchor='mm')
# cells
cells=[(253,403),(468,618),(683,833),(898,1048),(1112,1262),(1327,1478)]; cy0,cy1=1753,1904
parts=[('胸','main',2),('背','off',0),('肩','sub',1),('腕','sub',1),('腹','off',0),('脚','off',0)]
for (x0,x1),(ja,st,n) in zip(cells,parts):
    if st!='off':
        d.rounded_rectangle([M+x0+4,M+cy0+4,M+x1-4,M+cy1-4],radius=8,fill=hx('C9492F') if st=='main' else hx('F1C9B8'))
    tc=hx('FFF8EF') if st=='main' else hx('5E1D0F') if st=='sub' else hx('B5ABA3')
    mx=M+(x0+x1)/2
    d.text((mx,M+cy0+62),ja,font=fk,fill=tc,anchor='mm')
    for k in range(n):
        dx=mx+(k-(n-1)/2)*26
        d.ellipse([dx-8,M+cy1-40,dx+8,M+cy1-24],fill=tc)
# gauge
segs=[432,521,610,699,788,877,965,1054,1143,1232]; on=8
for i,x0 in enumerate(segs[:on]):
    d.rectangle([M+x0+4,M+2018,M+x0+58,M+2130],fill=hx('E8603F'))
fs=ImageFont.truetype(F+'MPLUS1p-Bold.ttf',36); fbig=ImageFont.truetype(F+'DelaGothicOne-Regular.ttf',72)
d.text((M+290,M+2068),'重量',font=fs,fill=hx('D8CABB'),anchor='mm')
d.text((M+1478,M+2050),'80',font=fbig,fill=hx('FFF8EF'),anchor='mm')
d.text((M+1478,M+2116),'/100kg',font=fs,fill=hx('D8CABB'),anchor='mm')
c.save('nb_card_training_full.png')
bg=Image.new('RGBA',c.size,hx('E9E6DF')); bg.alpha_composite(c)
bg.convert('RGB').resize((c.width//2,c.height//2),Image.LANCZOS).save('nb_card_training_preview.png')
print(c.size)
