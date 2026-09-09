import './JourneyPoster.css';
export function JourneyPoster({cities, rank=1}:{cities:string[];rank?:number}){
 const codes=cities.map(c=>c.replace(/[^A-Za-z]/g,'').slice(0,3).toUpperCase());
 return <div className={`journey-poster journey-poster--${((rank-1)%4)+1}`} aria-hidden="true">
  <div className="journey-poster__orb journey-poster__orb--a"/><div className="journey-poster__orb journey-poster__orb--b"/>
  <div className="journey-poster__grain"/><div className="journey-poster__label">CURATED ROUTE / {String(rank).padStart(2,'0')}</div>
  <div className="journey-poster__codes">{codes.map((c,i)=><span key={c+i}>{c}{i<codes.length-1&&<b>↗</b>}</span>)}</div>
  <div className="journey-poster__city">{cities[0] || 'Somewhere new'}</div>
  <div className="journey-poster__stamp">DETOURA<br/>FIELD NOTE</div>
 </div>
}
