import React from 'react';

type IconProps = {size?:number; className?:string};
const svgProps = (size:number, className?:string):React.SVGProps<SVGSVGElement> => ({
  width:size, height:size, viewBox:'0 0 24 24', fill:'none', stroke:'currentColor',
  strokeWidth:1.8, strokeLinecap:'round', strokeLinejoin:'round', className, style:{verticalAlign:'-2px'},
});

export const ArrowUp = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M12 19V5"/><path d="m5 12 7-7 7 7"/></svg>;
export const ArrowDown = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>;
export const ArrowRight = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>;
export const ArrowUpRight = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>;
export const Plus = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M12 5v14"/><path d="M5 12h14"/></svg>;
export const X = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>;
export const Trash = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg>;
export const Refresh = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>;
export const Search = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>;
export const Bag = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M6 8h12l-1 12H7L6 8z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/></svg>;
export const Tag = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M3 3h8.5L21 12.5 12.5 21 3 11.5V3z"/><circle cx="7.5" cy="7.5" r="1"/></svg>;
export const Activity = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M22 12h-4l-3 8L9 4l-3 8H2"/></svg>;
export const Chart = ({size=16, className}:IconProps) => <svg {...svgProps(size, className)}><path d="M5 20v-7"/><path d="M12 20V5"/><path d="M19 20v-4"/></svg>;
export const Spinner = ({size=16, className}:IconProps) => <svg {...svgProps(size)} className={`spin${className?` ${className}`:''}`} strokeWidth={2}><circle cx="12" cy="12" r="9" strokeDasharray="42 15"/></svg>;
