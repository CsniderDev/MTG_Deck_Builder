import React, { useState } from 'react';
import { MagicCard, ScryfallImageUris } from '../types'; 
interface MagicCardProps {
  card: MagicCard;
}

const MagicCardItem: React.FC<MagicCardProps> = ({ card }) => {
  const [isHovered, setIsHovered] = useState<boolean>(false);

  // Helper to handle DFCs (Double-Faced Cards) logic safely
  const getImageUrl = (size: keyof ScryfallImageUris): string => {
    if (card.image_uris) {
      return card.image_uris[size];
    }
    // Fallback to the front face for DFCs
    return card.card_faces?.[0]?.image_uris?.[size] || '';
  };

  return (
    <div 
      className="card-container"
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      <img 
        src={getImageUrl('normal')} 
        alt={card.name} 
        className="card-image" 
        loading="lazy"
      />

      {isHovered && (
        <div className="card-hover-preview">
          <img src={getImageUrl('large')} alt={card.name} />
        </div>
      )}
    </div>
  );
};

export default MagicCardItem;