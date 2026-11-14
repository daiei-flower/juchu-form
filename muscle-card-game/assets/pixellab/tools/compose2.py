from PIL import Image, ImageDraw, ImageFont
G='gen/'; FONT='fonts/DotGothic16-Regular.ttf'; S=2; M=8
def L(n): return Image.open(G+n).convert('RGBA')
def crop(im): return im.crop(im.getbbox())
def hx(h,a=255): h=h.lstrip('#'); return (int(h[:2],16),int(h[2:4],16),int(h[4:],16),a)
GEO={
 'training':dict(art=(25,23,152,100),text=(21,140,179,188),cells=[(22,195,44,213),(49,195,72,213),(76,195,99,213),(103,195,126,213),(130,195,153,213),(157,195,179,213)],
                 gauge=(23,249,178,257),wlabel=(24,232),wright=(176,232)),
 'mental':dict(art=(30,33,140,98),text=(30,152,170,250)),
 'method':dict(art=(30,38,140,99),text=(36,160,164,242)),
}
def card(kind,name,cost,art,eq,gem,label,effect,parts=None,weight=None):
    g=GEO[kind]; fr=L(f'frame_{kind}.png')
    c=Image.new('RGBA',(200+2*M,280+2*M),(0,0,0,0)); c.alpha_composite(fr,(M,M))
    ax,ay,aw,ah=g['art']; a=L(art)
    c.alpha_composite(a.crop((0,0,aw,ah)),(M+ax,M+ay))
    d=ImageDraw.Draw(c)
    d.rectangle([M+ax,M+ay,M+ax+aw-1,M+ay+14],fill=hx('1A1613',215))
    d.rectangle([M+ax,M+ay+ah-12,M+ax+aw-1,M+ay+ah-1],fill=hx('1A1613',200))
    if eq:
        ic=crop(L(eq))
        if ic.height>12: ic=ic.crop((0,(ic.height-12)//2,ic.width,(ic.height-12)//2+12))
        c.alpha_composite(ic,(M+ax+aw-ic.width-3,M+ay+2))
    o=crop(L('cost_orb.png')); c.alpha_composite(o,(M-4,M-4))
    gm=crop(L(gem))
    big=c.resize((c.width*S,c.height*S),Image.NEAREST); bd=ImageDraw.Draw(big); bd.fontmode='1'
    f32=ImageFont.truetype(FONT,32); f16=ImageFont.truetype(FONT,16)
    ow,oh=o.size
    bd.text(((M-4+ow/2)*S,(M-4+oh/2)*S+2),str(cost),font=f32,fill=hx('2B2622'),anchor='mm')
    nx=M+max(ax,ow-2)+4
    bd.text((nx*S,(M+ay+7.5)*S),name,font=f16 if False else ImageFont.truetype(FONT,24),fill=hx('FFF8EF'),anchor='lm')
    gs=gm.resize((max(1,gm.width*16//gm.height),16),Image.NEAREST) if gm.height>16 else gm.resize((gm.width*S,gm.height*S),Image.NEAREST)
    cx=(M+ax+aw/2)*S; tw=bd.textlength(label,font=f16)+gs.width+6
    big.alpha_composite(gs,(int(cx-tw/2),int((M+ay+ah-6)*S-gs.height/2)))
    bd.text((cx-tw/2+gs.width+6,(M+ay+ah-6)*S),label,font=f16,fill=hx('FFF8EF'),anchor='lm')
    x0,y0,x1,y1=g['text']; cy=(M+(y0+y1)/2)*S
    for i,ln in enumerate(effect):
        bd.text(((M+(x0+x1)/2)*S,cy+(i-(len(effect)-1)/2)*38),ln,font=f32,fill=hx('1E1A17'),anchor='mm')
    if parts:
        for (ja,st,n),(a0,b0,a1,b1) in zip(parts,g['cells']):
            if st!='off':
                bd.rectangle([(M+a0+3)*S,(M+b0+3)*S,(M+a1-2)*S-1,(M+b1-2)*S-1],fill=hx('C9492F') if st=='main' else hx('F1C9B8'))
            tc=hx('FFF8EF') if st=='main' else hx('5E1D0F') if st=='sub' else hx('8C7F73')
            mx=(M+(a0+a1)/2)*S
            bd.text((mx,(M+b0+8)*S),ja,font=f16,fill=tc,anchor='mm')
            for k in range(n):
                dx=mx+(k-(n-1)/2)*8
                bd.rectangle([dx-2,(M+b1-5)*S,dx+1,(M+b1-5)*S+3],fill=tc)
    if weight:
        cur,goal=weight; on=round(cur/goal*10); gx0,gy0,gx1,gy1=g['gauge']; step=(gx1-gx0+2)/10
        for i in range(on):
            s0=gx0+i*step
            bd.rectangle([int((M+s0+1)*S),(M+gy0+2)*S,int((M+s0+step-3)*S)-1,(M+gy1-2)*S-1],fill=hx('E8603F'))
        lx,ly=g['wlabel']; rx,ry=g['wright']
        bd.text(((M+lx)*S,(M+ly)*S),'重量',font=f16,fill=hx('D8CABB'),anchor='lm')
        s2=f' / {goal}kg'; w2=bd.textlength(s2,font=f16)
        bd.text(((M+rx)*S-w2,(M+ry)*S+2),s2,font=f16,fill=hx('D8CABB'),anchor='lm')
        bd.text(((M+rx)*S-w2,(M+ry)*S),str(cur),font=f32,fill=hx('FFF8EF'),anchor='rm')
    return big
cards=[card('training','ベンチプレス',2,'art_bench_press.png','icon_barbell.png','gem_common.png','トレーニング',['8ダメージ','相手に動揺2'],
            parts=[('胸','main',2),('背','off',0),('肩','sub',1),('腕','sub',1),('腹','off',0),('脚','off',0)],weight=(80,100)),
       card('mental','深呼吸',1,'art_deep_breath.png',None,'gem_common.png','メンタル',['フォーカス5']),
       card('method','ホエイプロテイン',1,'art_whey_protein.png',None,'gem_uncommon.png','メソッド',['毎ターン、','フォーカス+3'])]
pad=24; cw,ch=cards[0].size
out=Image.new('RGBA',(pad*4+cw*3,pad*2+ch),hx('E9E6DF'))
for i,b in enumerate(cards): out.alpha_composite(b,(pad+i*(cw+pad),pad))
out.save('card_preview_pixellab_v2.png'); print(out.size)
