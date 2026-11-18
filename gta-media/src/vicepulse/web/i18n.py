"""Website text in English and Brazilian Portuguese. Keys are shared; a missing PT key falls back to EN."""

from __future__ import annotations

LANGS = ("en", "pt")
LANG_CODE = {"en": "en", "pt": "pt-BR"}

T: dict[str, dict[str, str]] = {
    "en": {
        "nav.home": "Home", "nav.styles": "Styles", "nav.how": "How it works", "nav.pricing": "Pricing",
        "nav.login": "Sign in", "nav.signup": "Get started", "nav.dashboard": "Dashboard", "nav.logout": "Sign out",
        "hero.kicker": "Gaming clips, made for creators",
        "hero.title": "Your best moments, edited and ready for every feed.",
        "hero.lead": "VicePulse finds the real highlights in your streams and gameplay, edits them in your style "
                     "with your branding, adds captions, and delivers vertical clips for Shorts, Reels and TikTok.",
        "hero.cta": "Create your creator account", "hero.cta2": "See the four styles",
        "trust.rights": "Only with your written permission", "trust.real": "Effects follow real moments, never invented",
        "trust.private": "Nothing is posted without approval",
        "styles.title": "Four editing styles", "styles.lead": "Pick one per channel. Every effect is triggered by "
                        "something that really happened in the clip: a loud moment, a reaction, a big play.",
        "style.cinematic": "Cinematic", "style.cinematic.d": "Letterbox bars, round facecam window, slow push-ins, "
                           "slow motion on the big moment when nobody is talking, warm film grade.",
        "style.viral": "Funny / Viral", "style.viral.d": "Camera on top with a bold animated frame, punch-in zooms "
                       "and impact flashes on every strong moment, slanted sticker captions.",
        "style.clean": "Clean Professional", "style.clean.d": "The whole picture on a soft blurred background, "
                       "rounded corners, thin frame, lower-third name bar. Your facecam stays exactly where it is.",
        "style.pro": "Streamer Pro", "style.pro.d": "Your brand everywhere: moving pattern background, animated "
                     "facecam frame, title bar, logo badge, a MOMENT tag when the payoff hits.",
        "how.title": "How it works",
        "how.1": "Connect", "how.1.d": "Create an account and give us written permission for the channels and "
                 "platforms you choose. You can withdraw it at any time.",
        "how.2": "Detect", "how.2.d": "Live or after the stream, VicePulse listens for loud moments, reactions and "
                 "big action, and finds where your facecam is.",
        "how.3": "Edit", "how.3.d": "Clips are cut into a story (hook, context, payoff), framed for 9:16, "
                 "captioned from your real speech and branded with your logo.",
        "how.4": "Check and deliver", "how.4.d": "Every clip passes quality checks (sound level, length, no black "
                 "screens). You review it in your dashboard before anything is posted.",
        "platforms.title": "Made for every vertical feed",
        "platforms.lead": "YouTube Shorts, Instagram Reels, Facebook Reels and TikTok. Live sources: Twitch, YouTube "
                          "and Kick (through a second OBS output to VicePulse).",
        "pricing.title": "Plans", "pricing.lead": "Prices are a starting point while VicePulse is in early access. "
                         "No card is needed and nothing is charged online yet.",
        "pricing.month": "/ month", "pricing.clips": "clips per month", "pricing.feeds": "live channels",
        "pricing.styles": "Styles", "pricing.choose": "Choose", "pricing.free": "Free",
        "feat.watermark": "Small VicePulse mark", "feat.resolution": "Resolution", "feat.captions": "Captions",
        "feat.branding": "Your logo, colours and frames", "feat.live_clipping": "Live clipping",
        "feat.publishing": "Posting to your channels", "feat.analytics": "Analytics", "feat.support": "Support",
        "footer.rights": "VicePulse is an independent creator service. Not affiliated with Rockstar Games, "
                         "Take-Two, Twitch, YouTube, Meta, TikTok or Kick.",
        "footer.privacy": "Privacy", "footer.terms": "Terms", "footer.lang": "Português",
        "legal.draft": "Draft. This document has not been reviewed by a lawyer yet and may change before launch.",
        "auth.email": "Email", "auth.password": "Password", "auth.name": "Creator or channel name",
        "auth.language": "Dashboard language", "auth.accept": "I accept the terms and the privacy policy",
        "auth.signup": "Create account", "auth.login": "Sign in", "auth.have": "Already have an account?",
        "auth.new": "New to VicePulse?", "auth.bad": "Wrong email or password, or too many attempts. Try again "
                    "in a few minutes.", "auth.pw_hint": "At least 10 characters.",
        "dash.title": "Dashboard", "dash.overview": "Overview", "dash.clips": "Clips", "dash.branding": "Branding",
        "dash.prefs": "Editing", "dash.perms": "Permissions", "dash.analytics": "Analytics", "dash.plan": "Plan",
        "dash.account": "Account", "dash.staff": "Staff",
        "dash.welcome": "Welcome", "dash.status.pending": "Your account is waiting for permission checks. "
                        "Add your permissions so we can start.", "dash.status.active": "Your account is active.",
        "dash.status.suspended": "Your account is paused. Contact VicePulse.",
        "dash.usage": "Clips this month", "dash.left": "left", "dash.recent": "Latest clips",
        "dash.none": "No clips yet. They appear here as soon as they are ready.",
        "dash.checklist": "Getting started", "check.perm": "Add a permission record",
        "check.brand": "Upload your logo and colours", "check.style": "Choose your editing style",
        "check.verified": "VicePulse verified your permission",
        "brand.name": "Name on the clips", "brand.handle": "Handle", "brand.monogram": "Monogram (1-3 letters)",
        "brand.colours": "Colours", "brand.primary": "Primary", "brand.secondary": "Secondary",
        "brand.accent": "Accent", "brand.dark": "Dark", "brand.frame": "Facecam frame", "brand.radius": "Corner radius",
        "brand.thickness": "Thickness", "brand.outro": "End line (e.g. Follow for more)", "brand.logo": "Logo",
        "brand.upload": "Upload logo", "brand.logo_hint": "PNG, JPEG or WebP, up to 5 MB. Square works best.",
        "frame.gradient": "Gradient", "frame.solid": "Solid", "frame.neon": "Neon", "frame.minimal": "Minimal",
        "prefs.style": "Editing style", "prefs.layout": "Framing", "layout.auto": "Automatic",
        "layout.crop": "Follow the action (crop)", "layout.blur": "Whole picture on blurred background",
        "prefs.captions": "Burn in captions", "prefs.caption_language": "Caption language",
        "prefs.platforms": "Platforms", "prefs.facecam": "Facecam", "prefs.facecam_hint": "auto = detected for you; "
                           "none = no facecam; or x,y,w,h as fractions of the frame.",
        "prefs.locked": "not in your plan",
        "perm.title": "Permission records", "perm.lead": "Tell us exactly what VicePulse may do with your content. "
                      "We check each record by hand before using anything. You can withdraw a permission at any time "
                      "and it stops new clips at once.",
        "perm.scope": "Permission", "perm.platforms": "Platforms", "perm.evidence": "Link to your written permission "
                      "or signed licence (https)", "perm.note": "Notes (conditions, credit text)",
        "perm.add": "Add permission", "perm.revoke": "Withdraw", "perm.status": "Status",
        "scope.clip_creation": "Make clips from my streams and videos",
        "scope.vicepulse_channel_publishing": "Post clips on VicePulse channels (with credit)",
        "scope.creator_channel_publishing": "Post clips on my own channels for me",
        "scope.monetisation": "Monetised posts", "scope.branding": "Use my name and logo on clips",
        "scope.live_feed": "Receive my live stream (second OBS output)",
        "pstatus.requested": "Waiting for check", "pstatus.verified": "Verified", "pstatus.rejected": "Not accepted",
        "pstatus.revoked": "Withdrawn",
        "clips.title": "Your clips", "clips.preview": "Preview", "clips.download": "Download",
        "clips.approve": "Approve", "clips.changes": "Request changes", "clips.note": "What should change?",
        "dstatus.processing": "Processing", "dstatus.ready": "Ready for review", "dstatus.approved": "Approved",
        "dstatus.changes_requested": "Changes requested", "dstatus.withdrawn": "Withdrawn",
        "an.title": "Last 30 days", "an.clips": "Clips delivered", "an.approval": "Approval rate", "an.views": "Views",
        "an.by_style": "By style", "an.per_day": "Per day",
        "plan.current": "Current plan", "plan.request": "Request this plan", "plan.requested": "Plan request saved. "
                        "VicePulse will contact you; nothing has been charged.",
        "plan.nopay": "Online payments are not switched on. No card details are ever asked for on this site.",
        "acct.change_pw": "Change password", "acct.old": "Current password", "acct.new": "New password",
        "save": "Save", "saved": "Saved.", "back": "Back", "error": "Something went wrong",
        "staff.requests": "Permission requests to check", "staff.doc": "Link to the signed document (https)",
        "staff.verify": "Verify", "staff.reject": "Reject",
        "notfound": "Page not found",
    },
    "pt": {
        "nav.home": "Início", "nav.styles": "Estilos", "nav.how": "Como funciona", "nav.pricing": "Planos",
        "nav.login": "Entrar", "nav.signup": "Começar", "nav.dashboard": "Painel", "nav.logout": "Sair",
        "hero.kicker": "Cortes de games feitos para criadores",
        "hero.title": "Seus melhores momentos, editados e prontos para todos os feeds.",
        "hero.lead": "A VicePulse encontra os destaques reais das suas lives e gameplays, edita no seu estilo com a "
                     "sua marca, coloca legendas e entrega cortes verticais para Shorts, Reels e TikTok.",
        "hero.cta": "Criar conta de criador", "hero.cta2": "Ver os quatro estilos",
        "trust.rights": "Só com a sua autorização por escrito", "trust.real": "Efeitos seguem momentos reais, nunca inventados",
        "trust.private": "Nada é publicado sem aprovação",
        "styles.title": "Quatro estilos de edição", "styles.lead": "Escolha um por canal. Cada efeito nasce de algo "
                        "que aconteceu de verdade no corte: um momento alto, uma reação, uma jogada grande.",
        "style.cinematic": "Cinematográfico", "style.cinematic.d": "Faixas pretas, facecam redonda, aproximações "
                           "suaves, câmera lenta no grande momento quando ninguém está falando, cor de cinema.",
        "style.viral": "Engraçado / Viral", "style.viral.d": "Câmera em cima com moldura animada forte, zooms e "
                       "flashes de impacto em cada momento forte, legendas tipo adesivo.",
        "style.clean": "Profissional Limpo", "style.clean.d": "A imagem inteira sobre fundo desfocado, cantos "
                       "arredondados, moldura fina e barra de nome. Sua facecam fica exatamente onde está.",
        "style.pro": "Streamer Pro", "style.pro.d": "Sua marca em tudo: fundo animado, moldura animada na facecam, "
                     "barra de título, selo com logo e a etiqueta MOMENT quando o lance acontece.",
        "how.title": "Como funciona",
        "how.1": "Conecte", "how.1.d": "Crie sua conta e dê autorização por escrito para os canais e plataformas que "
                 "você escolher. Você pode retirar quando quiser.",
        "how.2": "Detecta", "how.2.d": "Ao vivo ou depois da live, a VicePulse identifica momentos altos, reações e "
                 "muita ação, e encontra onde está a sua facecam.",
        "how.3": "Edita", "how.3.d": "Os cortes viram uma história (gancho, contexto, clímax), em 9:16, com legendas "
                 "da sua fala real e a sua logo.",
        "how.4": "Confere e entrega", "how.4.d": "Todo corte passa por checagens de qualidade (volume, duração, sem "
                 "tela preta). Você revisa no painel antes de qualquer publicação.",
        "platforms.title": "Feito para todos os feeds verticais",
        "platforms.lead": "YouTube Shorts, Instagram Reels, Facebook Reels e TikTok. Fontes ao vivo: Twitch, YouTube "
                          "e Kick (por uma segunda saída do OBS para a VicePulse).",
        "pricing.title": "Planos", "pricing.lead": "Os preços são um ponto de partida durante o acesso antecipado. "
                         "Não pedimos cartão e nada é cobrado online por enquanto.",
        "pricing.month": "/ mês", "pricing.clips": "cortes por mês", "pricing.feeds": "canais ao vivo",
        "pricing.styles": "Estilos", "pricing.choose": "Escolher", "pricing.free": "Grátis",
        "feat.watermark": "Pequena marca VicePulse", "feat.resolution": "Resolução", "feat.captions": "Legendas",
        "feat.branding": "Sua logo, cores e molduras", "feat.live_clipping": "Cortes ao vivo",
        "feat.publishing": "Publicação nos seus canais", "feat.analytics": "Estatísticas", "feat.support": "Suporte",
        "footer.rights": "A VicePulse é um serviço independente para criadores. Sem vínculo com Rockstar Games, "
                         "Take-Two, Twitch, YouTube, Meta, TikTok ou Kick.",
        "footer.privacy": "Privacidade", "footer.terms": "Termos", "footer.lang": "English",
        "legal.draft": "Rascunho. Este documento ainda não foi revisado por um advogado. A versão em português para "
                       "o Brasil (LGPD) está sendo preparada; abaixo está o rascunho em inglês.",
        "auth.email": "E-mail", "auth.password": "Senha", "auth.name": "Nome do criador ou canal",
        "auth.language": "Idioma do painel", "auth.accept": "Aceito os termos e a política de privacidade",
        "auth.signup": "Criar conta", "auth.login": "Entrar", "auth.have": "Já tem conta?",
        "auth.new": "Novo na VicePulse?", "auth.bad": "E-mail ou senha incorretos, ou tentativas demais. Tente de "
                    "novo em alguns minutos.", "auth.pw_hint": "Pelo menos 10 caracteres.",
        "dash.title": "Painel", "dash.overview": "Resumo", "dash.clips": "Cortes", "dash.branding": "Marca",
        "dash.prefs": "Edição", "dash.perms": "Autorizações", "dash.analytics": "Estatísticas", "dash.plan": "Plano",
        "dash.account": "Conta", "dash.staff": "Equipe",
        "dash.welcome": "Bem-vindo", "dash.status.pending": "Sua conta aguarda a checagem das autorizações. "
                        "Adicione suas autorizações para começarmos.", "dash.status.active": "Sua conta está ativa.",
        "dash.status.suspended": "Sua conta está pausada. Fale com a VicePulse.",
        "dash.usage": "Cortes neste mês", "dash.left": "restantes", "dash.recent": "Últimos cortes",
        "dash.none": "Nenhum corte ainda. Eles aparecem aqui assim que ficam prontos.",
        "dash.checklist": "Primeiros passos", "check.perm": "Adicionar uma autorização",
        "check.brand": "Enviar sua logo e cores", "check.style": "Escolher seu estilo de edição",
        "check.verified": "A VicePulse verificou sua autorização",
        "brand.name": "Nome nos cortes", "brand.handle": "@ do canal", "brand.monogram": "Monograma (1-3 letras)",
        "brand.colours": "Cores", "brand.primary": "Principal", "brand.secondary": "Secundária",
        "brand.accent": "Destaque", "brand.dark": "Escura", "brand.frame": "Moldura da facecam",
        "brand.radius": "Arredondamento", "brand.thickness": "Espessura", "brand.outro": "Frase final (ex.: Siga para mais)",
        "brand.logo": "Logo", "brand.upload": "Enviar logo", "brand.logo_hint": "PNG, JPEG ou WebP, até 5 MB. "
                      "Quadrada fica melhor.",
        "frame.gradient": "Degradê", "frame.solid": "Sólida", "frame.neon": "Neon", "frame.minimal": "Mínima",
        "prefs.style": "Estilo de edição", "prefs.layout": "Enquadramento", "layout.auto": "Automático",
        "layout.crop": "Seguir a ação (corte)", "layout.blur": "Imagem inteira com fundo desfocado",
        "prefs.captions": "Legendas na imagem", "prefs.caption_language": "Idioma das legendas",
        "prefs.platforms": "Plataformas", "prefs.facecam": "Facecam", "prefs.facecam_hint": "auto = detectada "
                           "automaticamente; none = sem facecam; ou x,y,w,h em frações da imagem.",
        "prefs.locked": "fora do seu plano",
        "perm.title": "Autorizações", "perm.lead": "Diga exatamente o que a VicePulse pode fazer com o seu conteúdo. "
                      "Conferimos cada registro manualmente antes de usar qualquer coisa. Você pode retirar uma "
                      "autorização a qualquer momento e novos cortes param na hora.",
        "perm.scope": "Autorização", "perm.platforms": "Plataformas", "perm.evidence": "Link da sua autorização por "
                      "escrito ou licença assinada (https)", "perm.note": "Observações (condições, crédito)",
        "perm.add": "Adicionar autorização", "perm.revoke": "Retirar", "perm.status": "Situação",
        "scope.clip_creation": "Fazer cortes das minhas lives e vídeos",
        "scope.vicepulse_channel_publishing": "Publicar cortes nos canais da VicePulse (com crédito)",
        "scope.creator_channel_publishing": "Publicar cortes nos meus próprios canais por mim",
        "scope.monetisation": "Publicações monetizadas", "scope.branding": "Usar meu nome e logo nos cortes",
        "scope.live_feed": "Receber minha live (segunda saída do OBS)",
        "pstatus.requested": "Aguardando checagem", "pstatus.verified": "Verificada", "pstatus.rejected": "Não aceita",
        "pstatus.revoked": "Retirada",
        "clips.title": "Seus cortes", "clips.preview": "Prévia", "clips.download": "Baixar",
        "clips.approve": "Aprovar", "clips.changes": "Pedir alterações", "clips.note": "O que deve mudar?",
        "dstatus.processing": "Processando", "dstatus.ready": "Pronto para revisão", "dstatus.approved": "Aprovado",
        "dstatus.changes_requested": "Alterações pedidas", "dstatus.withdrawn": "Retirado",
        "an.title": "Últimos 30 dias", "an.clips": "Cortes entregues", "an.approval": "Taxa de aprovação",
        "an.views": "Visualizações", "an.by_style": "Por estilo", "an.per_day": "Por dia",
        "plan.current": "Plano atual", "plan.request": "Pedir este plano", "plan.requested": "Pedido de plano salvo. "
                        "A VicePulse vai entrar em contato; nada foi cobrado.",
        "plan.nopay": "Pagamentos online não estão ativos. Este site nunca pede dados de cartão.",
        "acct.change_pw": "Trocar senha", "acct.old": "Senha atual", "acct.new": "Nova senha",
        "save": "Salvar", "saved": "Salvo.", "back": "Voltar", "error": "Algo deu errado",
        "staff.requests": "Autorizações para conferir", "staff.doc": "Link do documento assinado (https)",
        "staff.verify": "Verificar", "staff.reject": "Recusar",
        "notfound": "Página não encontrada",
    },
}


def t(lang: str, key: str) -> str:
    return T.get(lang, T["en"]).get(key) or T["en"].get(key) or key


def missing_keys() -> dict[str, list[str]]:
    return {lang: sorted(set(T["en"]) - set(T[lang])) for lang in LANGS if lang != "en"}
