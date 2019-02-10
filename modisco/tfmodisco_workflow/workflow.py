from __future__ import division, print_function, absolute_import
from collections import defaultdict, OrderedDict, Counter
import numpy as np
import itertools
import time
import sys
import h5py
import json
from . import seqlets_to_patterns
from .. import core
from .. import coordproducers
from .. import metaclusterers
from .. import util
from .. import value_provider


class TfModiscoResults(object):

    def __init__(self,
                 task_names,
                 multitask_seqlet_creation_results,
                 metaclustering_results,
                 metacluster_idx_to_submetacluster_results,
                 **kwargs):
        self.task_names = task_names
        self.multitask_seqlet_creation_results =\
                multitask_seqlet_creation_results
        self.metaclustering_results = metaclustering_results
        self.metacluster_idx_to_submetacluster_results =\
            metacluster_idx_to_submetacluster_results

        self.__dict__.update(**kwargs)

    @classmethod
    def from_hdf5(cls, grp, track_set):
        task_names = util.load_string_list(dset_name="task_names",
                                           grp=grp)
        multitask_seqlet_creation_results =\
            core.MultiTaskSeqletCreationResults.from_hdf5(
                grp=grp["multitask_seqlet_creation_results"],
                track_set=track_set)
        metaclustering_results =\
            metaclusterers.MetaclusteringResults.from_hdf5(
                grp["metaclustering_results"])
        metacluster_idx_to_submetacluster_results = OrderedDict()
        metacluster_idx_to_submetacluster_results_group =\
            grp["metacluster_idx_to_submetacluster_results"]
        for metacluster_idx in metacluster_idx_to_submetacluster_results_group:
            metacluster_idx_to_submetacluster_results[metacluster_idx] =\
             SubMetaclusterResults.from_hdf5(
                grp=metacluster_idx_to_submetacluster_results_group[
                     metacluster_idx],
                track_set=track_set)

        return cls(task_names=task_names,
                   multitask_seqlet_creation_results=
                    multitask_seqlet_creation_results,
                   metaclustering_results=metaclustering_results,
                   metacluster_idx_to_submetacluster_results=
                    metacluster_idx_to_submetacluster_results)

    def save_hdf5(self, grp):
        util.save_string_list(string_list=self.task_names, 
                              dset_name="task_names", grp=grp)
        self.multitask_seqlet_creation_results.save_hdf5(
            grp.create_group("multitask_seqlet_creation_results"))
        self.metaclustering_results.save_hdf5(
            grp.create_group("metaclustering_results"))

        metacluster_idx_to_submetacluster_results_group = grp.create_group(
                                "metacluster_idx_to_submetacluster_results")
        for idx in self.metacluster_idx_to_submetacluster_results:
            self.metacluster_idx_to_submetacluster_results[idx].save_hdf5(
                grp=metacluster_idx_to_submetacluster_results_group
                    .create_group("metacluster_"+str(idx))) 


class SubMetaclusterResults(object):

    def __init__(self, metacluster_size, activity_pattern,
                       seqlets, seqlets_to_patterns_result):
        self.metacluster_size = metacluster_size
        self.activity_pattern = activity_pattern
        self.seqlets = seqlets
        self.seqlets_to_patterns_result = seqlets_to_patterns_result

    @classmethod
    def from_hdf5(cls, grp, track_set):
        metacluster_size = int(grp.attrs['size'])
        activity_pattern = np.array(grp['activity_pattern'])
        seqlet_coords = util.load_seqlet_coords(dset_name="seqlets", grp=grp)
        seqlets = track_set.create_seqlets(coords=seqlet_coords)
        seqlets_to_patterns_result =\
            seqlets_to_patterns.SeqletsToPatternsResults.from_hdf5(
                grp=grp["seqlets_to_patterns_result"],
                track_set=track_set) 
        return cls(metacluster_size=metacluster_size,
                   activity_pattern=activity_pattern,
                   seqlets=seqlets,
                   seqlets_to_patterns_result=seqlets_to_patterns_result) 

    def save_hdf5(self, grp):
        grp.attrs['size'] = self.metacluster_size
        grp.create_dataset('activity_pattern', data=self.activity_pattern)
        util.save_seqlet_coords(seqlets=self.seqlets,
                                dset_name="seqlets", grp=grp)   
        self.seqlets_to_patterns_result.save_hdf5(
            grp=grp.create_group('seqlets_to_patterns_result'))


def prep_track_set(task_names, contrib_scores,
                    hypothetical_contribs, one_hot):
    contrib_scores_tracks = [
        core.DataTrack(
            name=key+"_contrib_scores",
            fwd_tracks=contrib_scores[key],
            rev_tracks=[x[::-1, ::-1] for x in 
                        contrib_scores[key]],
            has_pos_axis=True) for key in task_names] 
    hypothetical_contribs_tracks = [
        core.DataTrack(name=key+"_hypothetical_contribs",
                       fwd_tracks=hypothetical_contribs[key],
                       rev_tracks=[x[::-1, ::-1] for x in 
                                    hypothetical_contribs[key]],
                       has_pos_axis=True)
                       for key in task_names]
    onehot_track = core.DataTrack(
                        name="sequence", fwd_tracks=one_hot,
                        rev_tracks=[x[::-1, ::-1] for x in one_hot],
                        has_pos_axis=True)
    track_set = core.TrackSet(
                    data_tracks=contrib_scores_tracks
                    +hypothetical_contribs_tracks+[onehot_track])
    return track_set


class TfModiscoWorkflow(object):

    def __init__(self,
                 seqlets_to_patterns_factory=
                 seqlets_to_patterns.TfModiscoSeqletsToPatternsFactory(),
                 sliding_window_size=21, flank_size=10,
                 histogram_bins=100, percentiles_in_bandwidth=10, 
                 overlap_portion=0.5,
                 min_metacluster_size=100,
                 weak_threshold_for_counting_sign=0.99,
                 max_seqlets_per_metacluster=20000,
                 target_seqlet_fdr=0.05,
                 min_passing_windows_frac=0.005,
                 max_passing_windows_frac=0.2,
                 verbose=True,
                 #thresholding_func_producer=
                 # coordproducers.FdrThreshFromEmpiricalNull,
                 #min_seqlets_per_task deprecated;
                 # now called min_passing_windows_frac
                 min_seqlets_per_task=None):

        if (min_seqlets_per_task is not None):
            raise DeprecationWarning(
                "parameter min_seqlets_per_task is now controlled by param"
                +" min_passing_windows_frac, which defaults to 0.005")

        self.seqlets_to_patterns_factory = seqlets_to_patterns_factory
        self.sliding_window_size = sliding_window_size
        self.flank_size = flank_size
        self.histogram_bins = histogram_bins
        self.percentiles_in_bandwidth = percentiles_in_bandwidth
        self.overlap_portion = overlap_portion
        self.min_metacluster_size = min_metacluster_size
        self.target_seqlet_fdr = target_seqlet_fdr
        self.weak_threshold_for_counting_sign =\
            weak_threshold_for_counting_sign
        self.max_seqlets_per_metacluster = max_seqlets_per_metacluster
        self.min_passing_windows_frac = min_passing_windows_frac
        self.thresholding_function = thresholding_function
        self.verbose = verbose

        self.build()

    def build(self):
        
        self.overlap_resolver = core.SeqletsOverlapResolver(
            overlap_detector=core.CoordOverlapDetector(self.overlap_portion),
            seqlet_comparator=core.SeqletComparator(
                               value_provider=
                                value_provider.CoorScoreValueProvider()))

    def __call__(self, task_names, contrib_scores,
                       hypothetical_contribs, one_hot,
                       #null_dist should either be a dictionary
                       # or a callable
                       per_position_contrib_scores=None,
                       null_dist=FlipSignNullDist(
                         num_to_samp=10000, shuffle_pos=False,
                         seed=1234, num_breaks=100,
                         lower_null_percentile=20,
                         upper_null_percentile=80)):

        self.coord_producer = coordproducers.FixedWindowAroundChunks(
            sliding=self.sliding_window_size,
            flank=self.flank_size,
            suppress=(int(0.5*self.sliding_window_size)
                      + self.flank_size),
            target_fdr=target_seqlet_fdr,
            min_passing_windows_frac=self.min_passing_windows_frac,
            max_passing_windows_frac=self.max_passing_windows_frac,
            max_seqlets_total=None,
            verbose=self.verbose) 

        track_set = prep_track_set(
                        task_names=task_names,
                        contrib_scores=contrib_scores,
                        hypothetical_contribs=hypothetical_contribs,
                        one_hot=one_hot)

        if (per_position_contrib_scores is None):
            per_position_contrib_scores = OrderedDict([
                (x, [np.sum(s,axis=1) for s in contrib_scores[x]])
                for x in task_names])

        multitask_seqlet_creation_results = core.MultiTaskSeqletCreator(
            coord_producer=self.coord_producer,
            overlap_resolver=self.overlap_resolver)(
                task_name_to_score_track=per_position_contrib_scores,
                null_dist=null_dist,
                track_set=track_set)

        #find the weakest transformed threshold used across all tasks
        weakest_transformed_thresh = (min(
            [min(x.tnt_results.transformed_pos_threshold,
                 x.tnt_results.transformed_neg_threshold)
                 for x in (multitask_seqlet_creation_results.
                           task_name_to_coord_producer_results.values())]) -
            0.0000001) #subtract 1e-7 to avoid numerical issues
        print("Across all tasks, the weakest transformed threshold used"
              +" was: "+str(weakest_transformed_thresh))

        seqlets = multitask_seqlet_creation_results.final_seqlets
        print(str(len(seqlets))+" identified in total")
        if (len(seqlets) < 100):
            print("WARNING: you found relatively few seqlets."
                  +" Consider dropping target_seqlet_fdr") 

        if (self.weak_threshold_for_counting_sign is None):
            weak_threshold_for_counting_sign = weakest_transformed_thresh
        else:
            weak_threshold_for_counting_sign =\
                self.weak_threshold_for_counting_sign
        if (weak_threshold_for_counting_sign > weakest_transformed_thresh):
            print("Reducing weak_threshold_for_counting_sign to"
                  +" match weakest_transformed_thresh, from "
                  +str(weak_threshold_for_counting_sign)
                  +" to "+str(weakest_transformed_thresh))
            weak_threshold_for_counting_sign = weakest_transformed_thresh

        task_name_to_value_provider = OrderedDict([
            (task_name,
             TransformCentralWindowValueProvider(
                track_name=task_name+"_contrib_scores",
                central_window=self.sliding_window_size,
                val_transformer= 
                 coord_producer_results.tnt_results.val_transformer))
             for (task_name,coord_producer_results)
                 in (multitask_seqlet_creation_results.
                     task_name_to_coord_producer_results.items())])

        metaclusterer = metaclusterers.SignBasedPatternClustering(
                                min_cluster_size=self.min_metacluster_size,
                                task_name_to_value_provider=
                                    task_name_to_value_provider,
                                task_names=task_names,
                                threshold_for_counting_sign=
                                    weakest_transformed_thresh,
                                weak_threshold_for_counting_sign=
                                    weak_threshold_for_counting_sign)

        metaclustering_results = metaclusterer.fit_transform(seqlets)
        metacluster_indices = np.array(
            metaclustering_results.metacluster_indices)
        metacluster_idx_to_activity_pattern =\
            metaclustering_results.metacluster_idx_to_activity_pattern

        num_metaclusters = max(metacluster_indices)+1
        metacluster_sizes = [np.sum(metacluster_idx==metacluster_indices)
                              for metacluster_idx in range(num_metaclusters)]
        if (self.verbose):
            print("Metacluster sizes: ",metacluster_sizes)
            print("Idx to activities: ",metacluster_idx_to_activity_pattern)
            sys.stdout.flush()

        metacluster_idx_to_submetacluster_results = OrderedDict()

        for metacluster_idx, metacluster_size in\
            sorted(enumerate(metacluster_sizes), key=lambda x: x[1]):
            print("On metacluster "+str(metacluster_idx))
            if (self.max_seqlets_per_metacluster is None
                or self.max_seqlets_per_metacluster >= metacluster_size): 
                print("Metacluster size", metacluster_size)
            else:
                print("Metacluster size {0} limited to {1}".format(
                        metacluster_size, self.max_seqlets_per_metacluster))
            sys.stdout.flush()
            metacluster_activities = [
                int(x) for x in
                metacluster_idx_to_activity_pattern[metacluster_idx].split(",")]
            assert len(seqlets)==len(metacluster_indices)
            metacluster_seqlets = [
                x[0] for x in zip(seqlets, metacluster_indices)
                if x[1]==metacluster_idx][:self.max_seqlets_per_metacluster]
            relevant_task_names, relevant_task_signs =\
                zip(*[(x[0], x[1]) for x in
                    zip(task_names, metacluster_activities) if x[1] != 0])
            print('Relevant tasks: ', relevant_task_names)
            print('Relevant signs: ', relevant_task_signs)
            sys.stdout.flush()
            if (len(relevant_task_names) == 0):
                assert False, "This should not happen"
                sys.stdout.flush()
            
            seqlets_to_patterns = self.seqlets_to_patterns_factory(
                track_set=track_set,
                onehot_track_name="sequence",
                contrib_scores_track_names =\
                    [key+"_contrib_scores" for key in relevant_task_names],
                hypothetical_contribs_track_names=\
                    [key+"_hypothetical_contribs" for key in relevant_task_names],
                track_signs=relevant_task_signs,
                other_comparison_track_names=[])

            seqlets_to_patterns_result = seqlets_to_patterns(metacluster_seqlets)
            metacluster_idx_to_submetacluster_results[metacluster_idx] =\
                SubMetaclusterResults(
                    metacluster_size=metacluster_size,
                    activity_pattern=np.array(metacluster_activities),
                    seqlets=metacluster_seqlets,
                    seqlets_to_patterns_result=seqlets_to_patterns_result)

        return TfModiscoResults(
                 task_names=task_names,
                 multitask_seqlet_creation_results=
                    multitask_seqlet_creation_results,
                 metaclustering_results=metaclustering_results,
                 metacluster_idx_to_submetacluster_results=
                    metacluster_idx_to_submetacluster_results)
