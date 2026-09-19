import { BrandLogo } from "../ui/BrandLogo";
import './JourneyPoster.css';
export function JourneyPoster({cities}:{cities:string[];rank?:number}){
 return <div className="journey-poster">
  <span className="journey-poster__label">Your journey</span>
  <div className="journey-poster__cities">{cities.join(' → ') || 'Somewhere new'}</div>
  <BrandLogo />
 </div>
}
