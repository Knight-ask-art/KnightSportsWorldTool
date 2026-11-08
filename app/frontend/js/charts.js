/* Lightweight official-style charts.  No chart library: the app ships offline
   and a 500-point record should not pull a 200 KB runtime. */
(function(){
  'use strict';
  const NS='http://www.w3.org/2000/svg';
  const W=620, H=148, L=34, R=12, T=8, B=18;
  const PW=W-L-R, PH=H-T-B;

  function esc(v){ return String(v == null ? '' : v).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function fmtClock(sec){
    sec=Math.max(0, Math.round(+sec || 0));
    const m=Math.floor(sec/60), s=sec%60;
    return m + ':' + String(s).padStart(2,'0');
  }
  function fmtPace(p){
    if(p == null || !isFinite(+p) || +p <= 0) return '—';
    const sec=Math.round(+p*60);
    const m=Math.floor(sec/60), s=sec%60;
    return m + "'" + String(s).padStart(2,'0') + '"';
  }
  function fmtNum(v, digits){
    if(v == null || !isFinite(+v)) return '—';
    return (+v).toFixed(digits == null ? 0 : digits);
  }
  function pct(values, q){
    const arr=values.filter(v => v != null && isFinite(+v)).map(Number).sort((a,b)=>a-b);
    if(!arr.length) return null;
    const i=Math.max(0, Math.min(arr.length-1, Math.round((arr.length-1)*q)));
    return arr[i];
  }
  function svgOpen(){
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="数据图表">`;
  }
  function axis(svg, opts){
    const {yMin,yMax,yTicks,formatY,invert,seriesMin,seriesMax}=opts;
    const span=(yMax-yMin)||1;
    const y = v => invert
      ? T + PH * ((v-yMin)/span)
      : T + PH - PH * ((v-yMin)/span);
    const out=[];
    for(let i=0;i<yTicks;i++){
      const v=yMin + span*i/(yTicks-1);
      const yy=y(v);
      out.push(`<line class="dp-grid-line" x1="${L}" y1="${yy.toFixed(1)}" x2="${W-R}" y2="${yy.toFixed(1)}"/>`);
      out.push(`<text class="dp-axis" x="${L-5}" y="${(yy+3).toFixed(1)}" text-anchor="end">${esc(formatY(v))}</text>`);
    }
    // x axis ticks: five points from seriesMin to seriesMax (seconds)
    const xs=[];
    for(let i=0;i<5;i++){
      const t=seriesMin + (seriesMax-seriesMin)*i/4;
      const xx=L + PW*i/4;
      xs.push(`<text class="dp-axis" x="${xx.toFixed(1)}" y="${H-3}" text-anchor="${i===0?'start':(i===4?'end':'middle')}">${esc(fmtClock(t))}</text>`);
    }
    return {parts:out.concat(xs), y};
  }
  function empty(el, text){
    el.innerHTML=svgOpen()+`<text class="dp-empty" x="${W/2}" y="${H/2}" text-anchor="middle">${esc(text||'暂无数据')}</text></svg>`;
  }
  function baseSeries(series){
    return (Array.isArray(series)?series:[]).filter(s => s && isFinite(+s.t));
  }
  function lineChart(el, series, opts){
    if(!el) return;
    series=baseSeries(series);
    opts=opts||{};
    const key=opts.key||'pace';
    const vals=series.map(s => s[key]).filter(v => v != null && isFinite(+v));
    if(!vals.length){ empty(el, opts.empty); return; }
    let lo=Math.min(...vals), hi=Math.max(...vals);
    if(opts.invert){
      // Pace: a single 10 s stop can read 400 min/km and flatten the rest of
      // the run.  Frame the chart on a trimmed band and clip the few extremes
      // to the border, the way the official chart does.  With fewer than ~60
      // windows the 98th percentile *is* the maximum, so short runs need a
      // wider trim or one stop still owns the whole axis.
      const q = vals.length < 60 ? 0.10 : 0.02;
      const median = pct(vals, 0.5);
      const pLo=pct(vals, q);
      let pHi=pct(vals, 1 - q);
      // Two stops in a 15-window preview are already 13% of the sample, so
      // the percentile alone can still let one of them own the axis.  Cap the
      // slow end at 1.5x the median; the stop is clipped to the border and the
      // normal pace band stays readable.
      if(median != null && pHi != null) pHi = Math.min(pHi, median * 1.5);
      lo = Math.max(0.5, (pLo == null ? lo : pLo) * 0.92);
      hi = Math.max(lo + 1, (pHi == null ? hi : pHi) * 1.08);
    }else if(opts.tight){
      // Altitude is an absolute value, not a quantity that starts at zero.
      // A 97-110 m trace on a 0-117 m axis looks like a flat line; the
      // official app frames the run's own band instead.  Keep a small margin
      // so the first/last points are not glued to the border, and enforce a
      // minimum span so "fixed 25 m" still gets readable ticks.
      const span = hi - lo;
      const minSpan = Math.max(0.5, +opts.minSpan || 0);
      if(span < minSpan){
        const mid = (hi + lo) / 2;
        lo = mid - minSpan / 2;
        hi = mid + minSpan / 2;
      }else{
        const pad = Math.max(0.6, span * 0.12);
        lo -= pad;
        hi += pad;
      }
    }else{
      lo=Math.min(0, lo);
      hi=hi + (hi-lo)*0.06;
    }
    if(!(hi>lo)){ hi=lo+1; }
    const ax=axis(null,{yMin:lo,yMax:hi,yTicks:4,invert:!!opts.invert,
      formatY:opts.formatY||(v=>fmtNum(v)),seriesMin:series[0].t,
      seriesMax:series[series.length-1].t});
    const yy=ax.y;
    const xx=t => L + PW * ((t-series[0].t) / Math.max(1, series[series.length-1].t-series[0].t));
    const band=opts.band;
    let bandMarkup='';
    if(band && band[0] != null && band[1] != null){
      const a=yy(Math.max(lo,Math.min(hi, +band[0])));
      const b=yy(Math.max(lo,Math.min(hi, +band[1])));
      const top=Math.min(a,b), bot=Math.max(a,b);
      bandMarkup=`<rect class="dp-band" x="${L}" y="${top.toFixed(1)}" width="${PW}" height="${Math.max(1,bot-top).toFixed(1)}"/>`;
      if(opts.bandNote) bandMarkup+=`<text class="dp-axis" x="${W-R}" y="${(top-3).toFixed(1)}" text-anchor="end">${esc(opts.bandNote)}</text>`;
    }
    let d='', dots='';
    let started=false;
    series.forEach(s => {
      const v=s[key];
      if(v == null || !isFinite(+v)) return;
      const xx1=xx(+s.t), yy1=yy(Math.max(lo,Math.min(hi,+v)));
      d += (started?'L':'M') + xx1.toFixed(1) + ' ' + yy1.toFixed(1) + ' ';
      started=true;
      if(opts.invert && band && (v < Math.min(band[0],band[1]) || v > Math.max(band[0],band[1]))){
        dots += `<circle class="dp-dot bad" cx="${xx1.toFixed(1)}" cy="${yy1.toFixed(1)}" r="1.6"/>`;
      }
    });
    el.innerHTML=svgOpen()+bandMarkup+ax.parts.join('')+
      `<path class="${opts.area?'dp-area':''}" d="${opts.area?d+`L ${xx(series[series.length-1].t).toFixed(1)} ${T+PH} L ${xx(series[0].t).toFixed(1)} ${T+PH} Z`:''}"/>`+
      `<path class="${opts.lineClass||'dp-line'}" d="${d}"/>`+dots+'</svg>';
  }
  function barChart(el, series, opts){
    if(!el) return;
    series=baseSeries(series);
    opts=opts||{};
    const key=opts.key||'cadence';
    const clamp = opts.clamp || null;
    const fix = v => clamp ? Math.max(clamp[0], Math.min(clamp[1], +v)) : +v;
    const vals=series.map(s => s[key]).filter(v => v != null && isFinite(+v)).map(fix);
    if(!vals.length){ empty(el, opts.empty); return; }
    let lo=0, hi=Math.max(...vals);
    if(opts.band && opts.band[1] != null) hi=Math.max(hi, +opts.band[1]);
    hi=Math.max(1, hi + (hi-lo)*0.08);
    const ax=axis(null,{yMin:lo,yMax:hi,yTicks:4,invert:false,
      formatY:opts.formatY||(v=>fmtNum(v)),seriesMin:series[0].t,
      seriesMax:series[series.length-1].t});
    const yy=ax.y;
    const step=PW/Math.max(1, series.length);
    const bw=Math.max(1.5, Math.min(8, step*0.62));
    const band=opts.band;
    let bandMarkup='';
    if(band && band[0] != null && band[1] != null){
      const a=yy(Math.max(lo,Math.min(hi,+band[0])));
      const b=yy(Math.max(lo,Math.min(hi,+band[1])));
      bandMarkup=`<rect class="dp-band" x="${L}" y="${Math.min(a,b).toFixed(1)}" width="${PW}" height="${Math.max(1,Math.abs(a-b)).toFixed(1)}"/>`+
        `<line class="dp-band-line" x1="${L}" y1="${b.toFixed(1)}" x2="${W-R}" y2="${b.toFixed(1)}"/>`;
      if(opts.bandNote) bandMarkup+=`<text class="dp-axis" x="${W-R}" y="${(Math.min(a,b)-3).toFixed(1)}" text-anchor="end">${esc(opts.bandNote)}</text>`;
    }
    let bars='';
    series.forEach((s,i) => {
      const raw=s[key];
      if(raw == null || !isFinite(+raw)) return;
      const v=fix(raw);
      const x=L + step*i + (step-bw)/2;
      const y=yy(Math.max(lo,Math.min(hi,+v)));
      const bad=band && band[0] != null && +v < +band[0];
      bars+=`<rect class="dp-bar${bad?' bad':''}${key==='stride'?' stride':''}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1,T+PH-y).toFixed(1)}" rx="1"/>`;
    });
    el.innerHTML=svgOpen()+bandMarkup+ax.parts.join('')+bars+'</svg>';
  }
  function spark(el, series, key){
    if(!el) return;
    series=baseSeries(series);
    const vals=series.map(s=>s[key]).filter(v=>v != null && isFinite(+v));
    if(!vals.length){ el.innerHTML=''; el.hidden=true; return; }
    const lo=Math.min(...vals), hi=Math.max(...vals), span=(hi-lo)||1;
    const pts=series.map((s,i)=>{
      const v=s[key];
      if(v == null || !isFinite(+v)) return '';
      const x=4+(W-8)*i/Math.max(1,series.length-1);
      const y=H-4-(H-8)*((v-lo)/span);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).filter(Boolean).join(' ');
    el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline fill="none" stroke="var(--ink-blue)" stroke-width="2" points="${pts}"/></svg>`;
    el.hidden=false;
  }
  window.HXPCharts={lineChart,barChart,spark,fmtPace,fmtClock,fmtNum};
})();
