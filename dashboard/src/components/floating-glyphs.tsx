const glyphs = [
  'float-t top-20 -left-10 text-[18rem] text-tomo-ink',
  'float-o top-[40%] -right-20 text-[24rem] text-tomo-accent',
  'float-m bottom-[10%] left-0 text-[20rem] text-tomo-ink',
  'float-o-2 bottom-[-5%] right-10 text-[16rem] text-tomo-accent',
];

export const FloatingGlyphs = () => {
  return (
    <div aria-hidden="true" className="pointer-events-none fixed inset-0 overflow-hidden">
      {glyphs.map((className, index) => (
        <span key={`${index}-${className}`} className={`floating-glyph ${className}`}>
          {'tomo'[index]}
        </span>
      ))}
    </div>
  );
};
