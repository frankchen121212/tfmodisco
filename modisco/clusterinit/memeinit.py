from __future__ import division, absolute_import
import os
import numpy as np
from .. import util
from joblib import Parallel, delayed
from collections import Counter
import os


def run_meme(meme_command, input_file, outdir, nmotifs):
    os.system(meme_command+" "+input_file+" -dna -mod anr -nmotifs "
              +str(nmotifs)+"  -minw 6 -maxw 50 -oc "+outdir)


class InitClustererFactory(object):

    #need this function simply because the onehot track name might not be
    # known at the time when MemeInitClustererFactory is instantiated
    def set_onehot_track_name(self, onehot_track_name):
        self.onehot_track_name = onehot_track_name


class MemeInitClustererFactory(InitClustererFactory):

    def __init__(self, meme_command, base_outdir, num_seqlets_to_use,
                       nmotifs, **pwm_clusterer_kwargs):
        self.meme_command = meme_command
        self.base_outdir = base_outdir
        self.num_seqlets_to_use = num_seqlets_to_use 
        self.nmotifs = nmotifs
        self.call_count = 0 #to avoid overwriting for each metacluster
        self.pwm_clusterer_kwargs = pwm_clusterer_kwargs

    def __call__(self, seqlets):

        if (hasattr(self, "onehot_track_name")==False):
            raise RuntimeError("Please call set_onehot_track_name first")

        onehot_track_name = self.onehot_track_name

        outdir = self.base_outdir+"/metacluster"+str(self.call_count)
        self.call_count += 1
        os.makedirs(outdir, exist_ok=True)

        seqlet_fa_to_write = outdir+"/inp_seqlets.fa"
        seqlet_fa_fh = open(seqlet_fa_to_write, 'w') 
        if (len(seqlets) > self.num_seqlets_to_use):
            seqlets = [seqlets[x] for x in np.random.RandomState(1).choice(
                         np.arange(self.num_seqlets_to_use),
                         replace=False)]

        letter_order = "ACGT"
        for seqlet in seqlets:
            seqlet_fa_fh.write(">"+str(seqlet.coor.example_idx)+":"
                               +str(seqlet.coor.start)+"-"
                               +str(seqlet.coor.end)+"\n") 
            seqlet_onehot = seqlet[onehot_track_name].fwd
            seqlet_fa_fh.write("".join([letter_order[x] for x in
                                np.argmax(seqlet_onehot, axis=-1)])+"\n") 
        seqlet_fa_fh.close()

        run_meme(meme_command=self.meme_command,
                 input_file=seqlet_fa_to_write,
                 outdir=outdir, nmotifs=self.nmotifs) 

        motifs = parse_meme(outdir+"/meme.xml")
        return PwmClusterer(
                pwms=motifs, onehot_track_name=self.onehot_track_name,
                **self.pwm_clusterer_kwargs)
        
        

def parse_meme(meme_xml):
    import xml.etree.ElementTree as ET 
    tree = ET.parse(meme_xml)
    motifs_xml = tree.getroot().find("motifs").getchildren()
    motifs = []
    for motif_xml in motifs_xml:
        motif = []
        alphabet_matrix_xml = (motif_xml.find("scores").find("alphabet_matrix")
                               .getchildren())
        for matrix_row_xml in alphabet_matrix_xml:
            matrix_row = [float(x.text) for x in matrix_row_xml.getchildren()] 
            motif.append(matrix_row) 
        motifs.append(np.array(motif))
    return motifs


def get_max_across_sequences(onehot_seq, weightmat):
    fwd_pwm_scan_results = util.compute_pwm_scan(onehot_seq=onehot_seq,
                                         weightmat=weightmat)
    rev_pwm_scan_results = util.compute_pwm_scan(onehot_seq=onehot_seq,
                                         weightmat=weightmat[::-1, ::-1])
    return np.max(np.maximum(fwd_pwm_scan_results, rev_pwm_scan_results),
                  axis=-1)


class PwmClusterer(object):

    def __init__(self, pwms, min_logodds, n_jobs,
                 onehot_track_name, verbose=True):
        self.pwms = pwms
        self.min_logodds = min_logodds
        self.n_jobs = n_jobs
        self.verbose = verbose
        self.onehot_track_name = onehot_track_name

    def __call__(self, seqlets):

        onehot_track_name = self.onehot_track_name

        onehot_seqlets = np.array([x[onehot_track_name].fwd for x in seqlets]) 
        #do a motif scan on onehot_seqlets
        max_pwm_scores_perseq = np.array(Parallel(n_jobs=self.n_jobs)(
                                    delayed(get_max_across_sequences)(
                                     onehot_seqlets, pwm)
                                    for pwm in self.pwms))
        #map seqlets to best match motif if min > self.min_logodds 
        argmax_pwm = np.argmax(max_pwm_scores_perseq, axis=0)
        argmax_pwm_score = np.squeeze(
            np.take_along_axis(max_pwm_scores_perseq,
                               np.expand_dims(argmax_pwm, axis=0),
                               axis=0))
        print(max_pwm_scores_perseq.shape)
        print(argmax_pwm.shape)
        #seqlet_assigned is a boolean vector indicating whether the seqlet
        # was actually successfully assigned to a cluster
        seqlet_assigned = argmax_pwm_score > self.min_logodds
        print(seqlet_assigned.shape)
        
        #not all pwms may wind up with seqlets assigned to them; if this is
        # the case, then we would want to remap the cluster indices such
        # that every assigned cluster index gets a seqlet assigned to it
        argmax_pwm[seqlet_assigned==False] = -1
        seqlets_per_pwm = Counter(argmax_pwm)
        if (self.verbose):
            print("Of "+str(len(seqlets))+" seqlets, cluster assignments are:",
                  seqlets_per_pwm)
        pwm_cluster_remapping = dict([(x[1],x[0]) for x in
            enumerate(sorted(seqlets_per_pwm.keys(),
                             key=lambda x: -seqlets_per_pwm[x]))
            if seqlets_per_pwm[x[1]] > 0 and x[0] >= 0])

        final_seqlet_clusters = np.zeros(len(seqlets))
        #assign the remapped clusters for the seqlets that received assignment
        final_seqlet_clusters[seqlet_assigned] = np.array(
            [pwm_cluster_remapping[x] for x in argmax_pwm[seqlet_assigned]])
        #for all the unassigned seqlets, assign each to its own cluster 
        final_seqlet_clusters[seqlet_assigned==False] = np.array(
            range(len(pwm_cluster_remapping),
                  len(pwm_cluster_remapping)+sum(seqlet_assigned==False))) 

        final_seqlet_clusters = final_seqlet_clusters.astype("int")

        return final_seqlet_clusters
