"""Website stylesheet and the animated style previews (pure CSS, original artwork: gradients and shapes only)."""

CSS = r"""
@font-face{font-family:Anton;src:url(/static/fonts/Anton-Regular.ttf) format("truetype");font-display:swap}
@font-face{font-family:Archivo Black;src:url(/static/fonts/ArchivoBlack-Regular.ttf) format("truetype");font-display:swap}
:root{--bg:#0D0714;--bg2:#160D22;--card:#1B1129;--line:#2E2140;--text:#F4EEFB;--muted:#B3A6C6;--pink:#FF2D95;
--purple:#8B2CF5;--orange:#FF8A3D;--ok:#2FBF71;--no:#E5484D;--grad:linear-gradient(120deg,#FF2D95,#8B2CF5 55%,#FF8A3D)}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,"Segoe UI",Roboto,sans-serif}
a{color:var(--pink)}img{max-width:100%}
.wrap{max-width:1160px;margin:0 auto;padding:0 20px}
header.top{position:sticky;top:0;z-index:10;background:rgba(13,7,20,.86);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
header.top .wrap{display:flex;align-items:center;gap:22px;height:64px}
.brand{display:flex;align-items:center;gap:10px;color:var(--text);text-decoration:none;font-family:Anton,Impact,sans-serif;font-size:24px;letter-spacing:.5px}
.brand img{width:36px;height:36px;border-radius:50%;object-fit:cover;border:2px solid var(--pink)}
nav.main{display:flex;gap:18px;margin-left:auto;align-items:center;flex-wrap:wrap}
nav.main a{color:var(--muted);text-decoration:none;font-weight:600;font-size:15px}nav.main a:hover{color:var(--text)}
.btn{display:inline-block;padding:11px 20px;border-radius:12px;font-weight:700;text-decoration:none;border:0;cursor:pointer;font-size:15px}
.btn.primary{background:var(--grad);color:#fff;box-shadow:0 6px 24px rgba(255,45,149,.28)}
.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.btn.small{padding:7px 12px;font-size:14px;border-radius:9px}.btn.danger{background:var(--no);color:#fff}.btn.ok{background:var(--ok);color:#04150b}
.hero{padding:72px 0 40px;display:grid;grid-template-columns:1.1fr .9fr;gap:40px;align-items:center}
.kicker{color:var(--orange);font-weight:800;text-transform:uppercase;letter-spacing:2px;font-size:13px}
h1,h2,h3{font-family:Anton,Impact,sans-serif;font-weight:400;letter-spacing:.4px;line-height:1.08}
.hero h1{font-size:clamp(38px,6vw,64px);margin:10px 0 16px}
.grad-text{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
.lead{color:var(--muted);font-size:18px;max-width:620px}.cta{display:flex;gap:12px;flex-wrap:wrap;margin-top:26px}
.trust{display:flex;gap:18px;flex-wrap:wrap;margin-top:26px;color:var(--muted);font-size:14px}
.trust span:before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:8px}
section{padding:56px 0}section h2{font-size:clamp(30px,4vw,44px);margin:0 0 10px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin-top:28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px}
.card h3{font-size:24px;margin:14px 0 6px}.card p{color:var(--muted);font-size:14.5px;margin:0}
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin-top:28px;counter-reset:s}
.step{background:var(--bg2);border:1px solid var(--line);border-radius:18px;padding:20px;position:relative}
.step:before{counter-increment:s;content:counter(s);font-family:Anton,sans-serif;font-size:42px;color:transparent;-webkit-text-stroke:1px var(--pink)}
.step h3{margin:4px 0 6px;font-size:22px}.step p{color:var(--muted);font-size:14.5px;margin:0}
.plans{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin-top:28px}
.plan{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;display:flex;flex-direction:column;gap:10px}
.plan.hl{border:2px solid var(--pink);box-shadow:0 0 0 4px rgba(255,45,149,.12)}
.plan .price{font-family:Anton,sans-serif;font-size:42px}.plan .price small{font-family:system-ui;font-size:14px;color:var(--muted)}
.plan ul{list-style:none;padding:0;margin:0;color:var(--muted);font-size:14.5px}.plan li{padding:4px 0}.plan li:before{content:"✓ ";color:var(--ok)}
footer{border-top:1px solid var(--line);padding:30px 0;color:var(--muted);font-size:14px;margin-top:40px}
footer .wrap{display:flex;gap:20px;flex-wrap:wrap;align-items:center}footer a{color:var(--muted)}
.legal{max-width:820px}.legal h1{font-size:40px}.legal h2{font-size:26px;margin-top:30px}.legal h3{font-size:20px}
.note{padding:12px 16px;border-radius:12px;background:#2a1d10;border:1px solid #6a4517;color:#ffd9a8;margin:16px 0}
.msg{padding:12px 16px;border-radius:12px;background:#132a1d;border:1px solid #2f5a3d;margin:16px 0}
.msg.err{background:#2a1316;border-color:#5a2f35}
form.stack{display:flex;flex-direction:column;gap:14px;max-width:520px}
label{display:flex;flex-direction:column;gap:6px;font-weight:600;font-size:14px}label.inline{flex-direction:row;align-items:center;gap:10px;font-weight:500}
input,select,textarea{padding:11px 12px;border-radius:10px;border:1px solid var(--line);background:#0B0612;color:var(--text);font:inherit}
input[type=color]{padding:2px;height:42px;width:64px}input[type=checkbox]{width:18px;height:18px}
.hint{color:var(--muted);font-weight:400;font-size:13px}
.auth{max-width:460px;margin:50px auto;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:28px}
.auth h1{font-size:34px;margin:0 0 14px}
.dash{display:grid;grid-template-columns:220px 1fr;gap:26px;padding:28px 0}
.side{display:flex;flex-direction:column;gap:4px}.side a{padding:10px 12px;border-radius:10px;color:var(--muted);text-decoration:none;font-weight:600}
.side a.on,.side a:hover{background:var(--card);color:var(--text)}.side a.on{box-shadow:inset 3px 0 0 var(--pink)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;margin-bottom:18px}
.panel h2{font-size:28px;margin:0 0 12px}.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.kpi{background:var(--bg2);border:1px solid var(--line);border-radius:14px;padding:16px}.kpi b{display:block;font-family:Anton,sans-serif;font-size:34px;font-weight:400}
.kpi span{color:var(--muted);font-size:13px}.bar{height:10px;border-radius:99px;background:#2b1f3b;overflow:hidden}.bar i{display:block;height:100%;background:var(--grad)}
table{width:100%;border-collapse:collapse}td,th{padding:10px 8px;border-bottom:1px solid var(--line);text-align:left;font-size:14.5px;vertical-align:top}
th{color:var(--muted);font-weight:600}.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12.5px;border:1px solid var(--line)}
.pill.verified,.pill.approved,.pill.active{border-color:#2f5a3d;color:#7ee2a8}.pill.requested,.pill.ready,.pill.pending{border-color:#6a4517;color:#ffc88a}
.pill.rejected,.pill.revoked,.pill.changes_requested{border-color:#5a2f35;color:#ff9aa5}
.check li{list-style:none;padding:5px 0}.check li.done:before{content:"✓ ";color:var(--ok)}.check li:not(.done):before{content:"○ ";color:var(--muted)}
.clipgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.clip{background:var(--bg2);border:1px solid var(--line);border-radius:14px;padding:10px;text-decoration:none;color:var(--text)}
.clip video,.player video{width:100%;aspect-ratio:9/16;background:#000;border-radius:10px;display:block}
.player{max-width:380px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.swatches{display:flex;gap:14px;flex-wrap:wrap}.logo-prev{width:96px;height:96px;border-radius:50%;object-fit:cover;border:3px solid var(--pink)}
/* ---------------- animated style previews (phone mockups) */
.phone{position:relative;aspect-ratio:9/16;border-radius:18px;overflow:hidden;background:#000;border:1px solid var(--line)}
.game{position:absolute;inset:0;background:
 radial-gradient(circle at 70% 75%,rgba(255,138,61,.9) 0 6%,transparent 18%),
 linear-gradient(180deg,#1b2a4a 0 45%,#2c3d2a 45% 100%);animation:pan 7s ease-in-out infinite alternate}
.game:after{content:"";position:absolute;left:12%;right:12%;top:52%;height:6%;background:#444;transform:skewX(-20deg);box-shadow:0 40px 0 #333}
.boom{position:absolute;left:58%;top:58%;width:30%;aspect-ratio:1;border-radius:50%;background:radial-gradient(circle,#fff 0 12%,#FFD84A 25%,#FF6A1F 50%,transparent 70%);
 transform:scale(0);animation:boom 3.2s ease-out infinite}
@keyframes pan{to{transform:scale(1.12) translateX(-4%)}}@keyframes boom{0%,55%{transform:scale(0);opacity:0}62%{transform:scale(1.1);opacity:1}80%{opacity:.6}100%{transform:scale(1.5);opacity:0}}
.cam{position:absolute;background:linear-gradient(160deg,#5a4a7a,#2a2140);overflow:hidden}
.cam:before{content:"";position:absolute;left:30%;top:22%;width:40%;height:40%;border-radius:50%;background:#d9a882}
.cam:after{content:"";position:absolute;left:18%;top:62%;width:64%;height:60%;border-radius:40% 40% 0 0;background:#3b2d58}
.cap{position:absolute;left:8%;right:8%;bottom:16%;text-align:center;font-family:"Archivo Black",sans-serif;font-size:13px;color:#fff;text-shadow:0 2px 0 #000}
.cine .bars:before,.cine .bars:after{content:"";position:absolute;left:0;right:0;height:11%;background:#000;z-index:2;animation:bars 3.2s infinite}
.cine .bars:before{top:0}.cine .bars:after{bottom:0}@keyframes bars{0%,40%{height:0}55%,100%{height:11%}}
.cine .cam{left:8%;top:14%;width:28%;aspect-ratio:1;border-radius:50%;box-shadow:0 0 0 3px #FF8A3D}
.cine .game{filter:sepia(.25) saturate(1.2) contrast(1.1)}.cine .title{position:absolute;top:36%;left:0;right:0;text-align:center;font-family:Anton,sans-serif;letter-spacing:6px;color:#f4f4f4;font-size:15px;animation:blurin 3.2s infinite}
@keyframes blurin{0%{filter:blur(6px);opacity:0}20%,100%{filter:blur(0);opacity:1}}
.viral .cam{left:0;right:0;top:0;height:36%;border-bottom:5px solid;border-image:linear-gradient(90deg,#FF2D95,#FFD600,#8B2CF5) 1}
.viral .game{top:36%}.viral .sticker{position:absolute;left:12%;right:12%;bottom:18%;background:#FFD600;color:#120a1a;font-family:"Archivo Black",sans-serif;
 font-size:13px;padding:6px 8px;text-align:center;transform:skewX(-8deg) rotate(-2deg);animation:pop 1.6s infinite}
@keyframes pop{0%,100%{transform:skewX(-8deg) rotate(-2deg) scale(1)}10%{transform:skewX(-8deg) rotate(-2deg) scale(1.15)}}
@keyframes frame{to{filter:hue-rotate(360deg)}}.viral .phone-inner{animation:punch 3.2s infinite}@keyframes punch{0%,58%,72%,100%{transform:scale(1)}62%{transform:scale(1.12)}}
.clean{background:#1d1626}.clean .game{inset:22% 6% 30% 6%;border-radius:14px;box-shadow:0 0 0 2px rgba(255,255,255,.5)}
.clean .bg{position:absolute;inset:0;background:linear-gradient(180deg,#1b2a4a,#2c3d2a);filter:blur(14px);opacity:.6}
.clean .cam{left:8%;top:24%;width:20%;aspect-ratio:1;border-radius:6px}
.clean .lower{position:absolute;left:6%;bottom:22%;background:rgba(13,7,20,.9);border-left:4px solid #FF2D95;color:#fff;font-size:11px;font-weight:700;padding:5px 10px;border-radius:4px;animation:slide 3.2s infinite}
@keyframes slide{0%{transform:translateX(-120%)}12%,85%{transform:none}100%{transform:translateX(-120%)}}
.pro .bg{position:absolute;inset:0;background:repeating-linear-gradient(135deg,#1a0f2b 0 14px,#24133b 14px 28px);animation:stripes 2s linear infinite}
@keyframes stripes{to{background-position:40px 0}}
.pro .gamebox{position:absolute;left:5%;right:5%;top:44%;height:38%;border-radius:12px;overflow:hidden;border:3px solid #FF2D95}
.pro .gamebox .game{inset:0}
.pro .camwrap{position:absolute;left:14%;right:14%;top:12%;height:28%;border-radius:14px;padding:4px;background:conic-gradient(from var(--a,0deg),#FF2D95,#8B2CF5,#FF8A3D,#FF2D95);animation:spin 2.4s linear infinite}
.pro .camwrap .cam{position:relative;width:100%;height:100%;border-radius:11px}
@property --a{syntax:"<angle>";initial-value:0deg;inherits:false}@keyframes spin{to{--a:360deg}}
.pro .tbar{position:absolute;left:0;top:4%;padding:4px 14px 4px 10px;background:linear-gradient(90deg,#FF2D95,#8B2CF5);color:#fff;font-family:Anton,sans-serif;font-size:13px;
 clip-path:polygon(0 0,100% 0,92% 100%,0 100%);animation:slide 3.2s infinite}
.pro .tag{position:absolute;right:8%;top:46%;background:#FFD600;color:#120a1a;font-family:Anton,sans-serif;font-size:11px;padding:2px 8px;border-radius:4px;transform:translateY(-200%);opacity:0;animation:tag 3.2s infinite}
@keyframes tag{0%,58%{transform:translateY(-200%);opacity:0}64%,90%{transform:none;opacity:1}100%{opacity:0}}
.pro .badge{position:absolute;right:6%;bottom:8%;width:16%;aspect-ratio:1;border-radius:50%;background:var(--grad);display:grid;place-items:center;color:#fff;font-family:Anton,sans-serif;font-size:12px}
.hero>*,.cards>*,.steps>*,.plans>*,.dash>*,.kpis>*{min-width:0}h1,h2{overflow-wrap:break-word}
@media (max-width:980px){.hero{grid-template-columns:1fr}.cards,.steps,.plans{grid-template-columns:repeat(2,1fr)}.dash{grid-template-columns:1fr}.side{flex-direction:row;flex-wrap:wrap}}
@media (max-width:560px){.cards,.steps,.plans,.kpis{grid-template-columns:1fr}nav.main a:not(.btn){display:none}header.top .wrap{gap:10px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""


def preview(style: str, label: str = "", monogram: str = "VP") -> str:
    """One animated phone mockup for a style. Pure HTML/CSS, no video file needed."""
    cap = '<div class="cap">THAT JUMP WAS CRAZY</div>'
    if style == "cinematic":
        inner = (f'<div class="game"></div><div class="boom"></div><div class="bars"></div><div class="cam"></div>'
                 f'<div class="title">THE LAST CHANCE</div>{cap}')
    elif style == "viral":
        inner = ('<div class="phone-inner" style="position:absolute;inset:0"><div class="game"></div><div class="boom"></div>'
                 '<div class="cam"></div></div><div class="sticker">NO WAY HE MADE IT</div>')
    elif style == "clean":
        inner = ('<div class="bg"></div><div class="game"></div><div class="boom" style="left:60%;top:45%;width:22%"></div>'
                 f'<div class="cam"></div><div class="lower">{label or "@yourchannel"}</div>')
    else:
        inner = (f'<div class="bg"></div><div class="tbar">BIGGEST JUMP EVER</div><div class="camwrap"><div class="cam"></div></div>'
                 f'<div class="gamebox"><div class="game"></div><div class="boom"></div></div><div class="tag">MOMENT</div>'
                 f'<div class="badge">{monogram}</div>')
    return f'<div class="phone {style if style != "cinematic" else "cine"}" role="img" aria-label="{style} preview">{inner}</div>'
